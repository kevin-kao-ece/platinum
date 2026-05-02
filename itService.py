from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS
from pymongo import MongoClient
from logHelper import logger
from urllib.parse import quote_plus

# 請勿 pip install bson（會蓋掉 PyMongo 內建的 bson）；編碼長度用 bson.encode
from bson import encode as bson_encode

class ItServiceHandler:
    def __init__(self, cfg):
        self.cfg = cfg
        self._influx_client = None
        self._mongo_client = None
        try:
            self.influxDBOrg = cfg.get("influxdb", {}).get("org")
            self.influxDBBucket = cfg.get("influxdb", {}).get("bucket")
            self.influxDBMeasurement = cfg.get("influxdb", {}).get("measurement")
            influxDBClient = InfluxDBClient(
                url=cfg.get("influxdb", {}).get("url"),
                token=cfg.get("influxdb", {}).get("token"),
                org=cfg.get("influxdb", {}).get("org"),
            )
            self._influx_client = influxDBClient
            self.influxDBWriteApi = influxDBClient.write_api(write_options=SYNCHRONOUS)
            logger.info("influxDB connected.")

            mongoDB_host = cfg.get("mongodb", {}).get("host")
            mongoDB_port = cfg.get("mongodb", {}).get("port")
            mongoDB_username = quote_plus(cfg.get("mongodb", {}).get("user"))
            mongoDB_password = quote_plus(cfg.get("mongodb", {}).get("password"))
            mongoDB_database = cfg.get("mongodb", {}).get("database")
            mongoDB_collection = cfg.get("mongodb", {}).get("collection")

            mongodb_URL = f"mongodb://{mongoDB_username}:{mongoDB_password}@{mongoDB_host}:{mongoDB_port}/"
            mongodb_client = MongoClient(mongodb_URL)
            self._mongo_client = mongodb_client

            # Select database and collection
            mongodb = mongodb_client[mongoDB_database]
            self.mongoCollection = mongodb[mongoDB_collection]
            logger.info("mongodb connected.")
        except Exception as e:
            logger.error(f"Failed to connect to influxDB or mongodb: {e}")
            raise e

    def close(self) -> None:
        """關閉 Influx / Mongo 連線（軟重載前呼叫）。"""
        for name, obj in (
            ("influx write_api", getattr(self, "influxDBWriteApi", None)),
            ("influx client", self._influx_client),
            ("mongo client", self._mongo_client),
        ):
            if obj is None:
                continue
            try:
                close_fn = getattr(obj, "close", None)
                if callable(close_fn):
                    close_fn()
            except Exception as e:
                logger.warning("ItService close %s: %s", name, e)
        self.influxDBWriteApi = None
        self._influx_client = None
        self.mongoCollection = None
        self._mongo_client = None
    
    def insertMessageToInfluxDB(self, dataList):
        try:
            point = Point(self.influxDBMeasurement)
            for key, value in dataList.items():
                if key == "name":
                    point.tag(key, value)
                else:
                    point.field(key, value)
            
            logger.debug(point)
            self.influxDBWriteApi.write(bucket=self.influxDBBucket, org=self.influxDBOrg, record=point)
        except Exception as error:
            # 勿 raise：開機初期網路／後端未就緒時，raise 會讓 main 輪詢執行緒結束，PLC 也不再重試。
            logger.error("InfluxDB write failed: %s", error)

    def insertMessageToMongoDB(self, document):
        try:
            result = self.mongoCollection.insert_one(document)
            bson_size = len(bson_encode(document))
            logger.debug(f"Size of data: {bson_size} bytes")
        except Exception as error:
            logger.error("MongoDB insert failed: %s", error)

