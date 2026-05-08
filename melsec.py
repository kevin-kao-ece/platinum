import threading
import struct
import time
from pymcprotocol import Type3E, Type4E
from logHelper import logger

MAX_PLCS = 2

def normalize_plc_configs(cfg: dict) -> list[dict]:
    """
    回傳 1～2 個 PLC 設定（與 legacy 單一 `melsec` 區塊相同欄位）。
    優先使用 `melsecs`（list）；若無則使用 `melsec`（dict）。
    """
    raw_list = cfg.get("melsecs")
    if isinstance(raw_list, list) and raw_list:
        out: list[dict] = []
        for item in raw_list[:MAX_PLCS]:
            if isinstance(item, dict):
                out.append(item)
        if len(raw_list) > MAX_PLCS:
            logger.warning(
                "設定 melsecs 共有 %s 項，僅使用前 %s 台",
                len(raw_list),
                MAX_PLCS,
            )
        return out

    single = cfg.get("melsec")
    if isinstance(single, dict) and single:
        return [single]
    return []

def effective_poll_interval(plc_configs: list[dict], default: float = 3.0) -> float:
    """多 PLC 時取各台 poll_interval 的最小值（一輪結束後等待時間）。"""
    intervals: list[float] = []
    for p in plc_configs:
        raw = p.get("poll_interval", default)
        try:
            intervals.append(float(raw))
        except (TypeError, ValueError):
            intervals.append(default)
    return min(intervals) if intervals else default


def max_connections_from_config(plc_config: dict) -> int:
    """Shared parser for `melsec.max_connections` (PLC sessions = poll thread pool size)."""
    raw = plc_config.get("max_connections", 1)
    try:
        n = int(raw)
    except (TypeError, ValueError):
        n = 1
    return max(1, n)

class MelsecSession:
    """Single TCP session to the PLC (one Type3E/4E client)."""

    @staticmethod
    def _words_as_unsigned_le_bytes(data):
        """Word devices are 16-bit; pymcprotocol may return signed ints — normalize for struct.pack('<H')."""
        u = [(int(w) & 0xFFFF) for w in data]
        return struct.pack(f"<{len(u)}H", *u)

    def __init__(self, plc_config, session_id=0):
        self.session_id = session_id
        self.plc_name = str(plc_config.get("name") or "Unknown")
        self.ip = plc_config["ip"]
        self.port = plc_config.get("port", 1025)
        frame = plc_config.get("frame_type", "3E")
        self.plc = Type4E() if frame == "4E" else Type3E()
        self.lock = threading.Lock()
        self.is_connected = False
        try:
            self._connect_retries = max(1, int(plc_config.get("connect_retries", 12)))
        except (TypeError, ValueError):
            self._connect_retries = 12
        try:
            self._connect_retry_delay = float(plc_config.get("connect_retry_delay_sec", 1.5))
        except (TypeError, ValueError):
            self._connect_retry_delay = 1.5

        self.type_map = {
            "uint16": (1, "<H"),
            "int16": (1, "<h"),
            "uint32": (2, "<I"),
            "int32": (2, "<i"),
            "float": (2, "<f"),
            "double": (4, "<d"),
        }

    def _disconnect_transport(self) -> None:
        """連線失敗或錯誤後清掉底層 socket，避免残狀態阻擋下次 connect。"""
        self.is_connected = False
        try:
            close_fn = getattr(self.plc, "close", None)
            if callable(close_fn):
                close_fn()
        except Exception:
            pass

    def _ensure_connection(self):
        with self.lock:
            if self.is_connected:
                return True
            delay = self._connect_retry_delay
            last_err: Exception | None = None
            for attempt in range(self._connect_retries):
                try:
                    self._disconnect_transport()
                    self.plc.connect(self.ip, self.port)
                    self.is_connected = True
                    if attempt > 0:
                        logger.info(
                            "PLC [%s] session %s connected after %s attempts (%s:%s)",
                            self.plc_name,
                            self.session_id,
                            attempt + 1,
                            self.ip,
                            self.port,
                        )
                    return True
                except Exception as e:
                    last_err = e
                    logger.warning(
                        "PLC [%s] connect attempt %s/%s (session %s, %s:%s): %s",
                        self.plc_name,
                        attempt + 1,
                        self._connect_retries,
                        self.session_id,
                        self.ip,
                        self.port,
                        e,
                    )
                    self.is_connected = False
                    if attempt < self._connect_retries - 1:
                        time.sleep(delay)
                        delay = min(delay * 1.7, 20.0)
            logger.error(
                "PLC [%s] connect failed after %s attempts (session %s, %s:%s): %s",
                self.plc_name,
                self._connect_retries,
                self.session_id,
                self.ip,
                self.port,
                last_err,
            )
            return False

    def read(self, tagName, details):
        if not self._ensure_connection():
            return None
        device = details.get("device")
        dtype = details.get("datatype", "uint16")

        try:
            with self.lock:
                if dtype == "bool":
                    data = self.plc.batchread_bitunits(
                        headdevice=device, readsize=1
                    )
                    return bool(data[0]) if data else None

                elif dtype == "string":
                    length = details.get("length", 10)
                    data = self.plc.batchread_wordunits(
                        headdevice=device, readsize=length
                    )
                    raw_bytes = self._words_as_unsigned_le_bytes(data)
                    return raw_bytes.split(b"\x00")[0].decode(
                        "ascii", errors="ignore"
                    )

                elif dtype in self.type_map:
                    words_count, fmt = self.type_map[dtype]
                    data = self.plc.batchread_wordunits(
                        headdevice=device, readsize=words_count
                    )
                    if len(data) < words_count:
                        return None

                    raw_bytes = self._words_as_unsigned_le_bytes(data)
                    return struct.unpack(fmt, raw_bytes)[0]

                logger.error(
                    f"Read unsupported datatype [{device}]: {dtype!r}"
                )
                return None

        except Exception as e:
            logger.error(f"Read Error [{device}]: {e}")
            self.is_connected = False
            return None

    def write(self, tag, val):
        if not self._ensure_connection():
            return False
        device = tag["device"]
        dtype = tag.get("datatype", "uint16")

        try:
            with self.lock:
                if dtype == "bool":
                    self.plc.batchwrite_bitunits(
                        headdevice=device, values=[1 if val else 0]
                    )

                elif dtype == "string":
                    length = tag.get("length", 10)
                    encoded = val.encode("ascii")[: length * 2].ljust(
                        length * 2, b"\x00"
                    )
                    words = struct.unpack(f"<{length}H", encoded)
                    self.plc.batchwrite_wordunits(
                        headdevice=device, values=list(words)
                    )

                elif dtype in self.type_map:
                    words_count, fmt = self.type_map[dtype]
                    raw = struct.pack(fmt, val)
                    words = struct.unpack(f"<{words_count}H", raw)
                    self.plc.batchwrite_wordunits(
                        headdevice=device,
                        values=[int(w) & 0xFFFF for w in words],
                    )
                else:
                    logger.error(
                        f"Write unsupported datatype [{device}]: {dtype!r}"
                    )
                    return False
            return True
        except Exception as e:
            logger.error(f"Write Error [{device}]: {e}")
            self.is_connected = False
            return False

    def close(self) -> None:
        """關閉與 PLC 的 TCP 連線（軟重載時呼叫）。"""
        with self.lock:
            if not self.is_connected:
                return
            try:
                close_fn = getattr(self.plc, "close", None)
                if callable(close_fn):
                    close_fn()
            except Exception as e:
                logger.error(
                    "PLC [%s] session %s close: %s",
                    self.plc_name,
                    self.session_id,
                    e,
                )
            finally:
                self.is_connected = False


class MelsecHandler:
    """Routes read/write to one of several PLC sessions (TCP connections)."""

    def __init__(self, name, plc_config):
        self.name = name
        self.max_connections = max_connections_from_config(plc_config)
        self.sessions = [
            MelsecSession(plc_config, session_id=i)
            for i in range(self.max_connections)
        ]

    @staticmethod
    def _session_index(key: str, num_sessions: int) -> int:
        if num_sessions <= 1:
            return 0
        s = str(key)
        return sum(ord(c) for c in s) % num_sessions

    def read(self, tagName, details):
        idx = self._session_index(tagName, len(self.sessions))
        return self.sessions[idx].read(tagName, details)

    def write(self, tag, val):
        route_key = tag.get("device", "")
        idx = self._session_index(route_key, len(self.sessions))
        return self.sessions[idx].write(tag, val)

    def close(self) -> None:
        for s in self.sessions:
            s.close()
