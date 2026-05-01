import ctypes, json, subprocess, os, base64, sys
from pathlib import Path
from ctypes import wintypes
from cryptography import x509
from cryptography.x509 import ExtensionNotFound
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.x509.oid import ObjectIdentifier
from datetime import datetime, timezone
from logHelper import logger

# 授權相關檔案與 main.py / web.py 共用（不依賴當前工作目錄）
ARTIFACT_DIR = Path(__file__).resolve().parent
LICENSE_CRT_PATH = ARTIFACT_DIR / "license.crt"
# 舊版綁定檔（仍會嘗試讀取以相容升級）
LICENSE_META_PATH = ARTIFACT_DIR / "license_meta.json"
# CSR 產生後寫入之本機綁定資料（匯出 CSR 時不會提供此檔下載）
LICENSE_BINDING_PATH = ARTIFACT_DIR / "license.binding"
LICENSE_CSR_PATH = ARTIFACT_DIR / "license.csr"


def load_license_binding_meta() -> dict | None:
    """讀取 CSR 產生時寫入的綁定資料；優先 license.binding，其次舊版 license_meta.json。"""
    for path in (LICENSE_BINDING_PATH, LICENSE_META_PATH):
        try:
            if path.is_file():
                return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
    return None

NCRYPT_PAD_OAEP_FLAG = 0x00000002
MS_PLATFORM_CRYPTO_PROVIDER = "Microsoft Platform Crypto Provider"
PRODUCT_ID = "neoedgex_melsec_bridge"

# Server 憑證（原始 bytes，下方 normalize 後載入）
_ROOT_CA_PEM = b"""
    -----BEGIN CERTIFICATE-----
    MIIFnTCCA4WgAwIBAgIUNh4qSokElPD6zvNyrzq9dwXCI1QwDQYJKoZIhvcNAQEL
    BQAwXTELMAkGA1UEBhMCVFcxDzANBgNVBAgMBlRhaXdhbjEPMA0GA1UEBwwGVGFp
    cGVpMRMwEQYDVQQKDAplQ2xvdWRFZGdlMRcwFQYDVQQDDA5Qcm90b2NvbFJvb3RD
    QTAgFw0yNjAyMTExMzU0NTdaGA8yMDc2MDEzMDEzNTQ1N1owXTELMAkGA1UEBhMC
    VFcxDzANBgNVBAgMBlRhaXdhbjEPMA0GA1UEBwwGVGFpcGVpMRMwEQYDVQQKDApl
    Q2xvdWRFZGdlMRcwFQYDVQQDDA5Qcm90b2NvbFJvb3RDQTCCAiIwDQYJKoZIhvcN
    AQEBBQADggIPADCCAgoCggIBALTjMPFXRoyg2hwnlHFUxAHBUgsBGnok7qHCHHUr
    1em4PdrLHcoLk3w3yX1fF7pwWLZtQnrVXPzz4DNLKDOuQhua17G5LGdVdUj9SnyK
    WXzvEndYxBYXiS9RdXgVmW7gcS7Vg2xUPQEpgX8Pg3BckJ6+tAvJkx8Rix51Vkxn
    EVJRzMLbnHH6N2Rc82MO5zOBSGvLmPpcnKD2e2kZji6knI3bn6dUEiDH7mRjSABu
    JksYByWb21PhEQVk7dq2DOJKuMMiYuyR6cIwkoeolQbdYTxgp3gqU90MLdwxVmkA
    lYX0Mvu68KlPfP9lYlfKa/dHTnIZsvk/gYULts75Mi4tZaCD4GwaqZkkYzLQGNLj
    kZMPYM1mSsGfeqfHCV3cu1Tt+AEXeJWpWZeXctv4Qx4q8xrahSQ3kY3ujdyUq6IB
    QJaQGWsJTMw4G/2brMtkiUmMNoPFW92WNzqsKJUVhHhoTyK0DonPAWMu/LktpiK5
    EkbsXq36A6fkJXx4NLhb5t2m9y1aTRFLl6q3ULfiu+3qBMsf4mDlPCTe4u7EWovv
    E/veYo6yReB45WI5I3y4opYY4dcDllHO98Q1U3rb0j5DYqqHoimax44W3mA6V8GS
    JMC0tErsdUt/fKDrus8ZUJfCXfI0MMKgokMRgmZ/uXrSi3SzJfQTTd8HKwT+SJoo
    Y1dVAgMBAAGjUzBRMB0GA1UdDgQWBBTcpEEahMWP3BVvKwBlZTKsUcepITAfBgNV
    HSMEGDAWgBTcpEEahMWP3BVvKwBlZTKsUcepITAPBgNVHRMBAf8EBTADAQH/MA0G
    CSqGSIb3DQEBCwUAA4ICAQAUt++uxOl4albcFK1Fw3GURr7DKVUul7YBluryagJN
    R+W+4sLptnUdFbm3Kfl1fmDPzr/wWMqoMSTPbp3YPDmZ146+stlfGvBGMlPrMtZc
    FWYdnhsTNms1T3+m72LND+fafBsuMUyKV1MnEzKu17kF15PMRd7FTyzphhrhu0jt
    nQcI1ak36kjM4xuCUBNyTSo/dOWbXFVc7nEjxQ0Ygo819Don4fvETG8bATzmQDEe
    cApqyfALhN1vjpRe7AyrTMlZ3nEAfccOupnDd+A8lDZP3/8rmk0QRJ8GIlpJST37
    LHWGEfSwVNkOmzW217wwGlN4dXeEHjDzEIHKAJ4m8Y24aI84zGk9g3cPVATbUa0g
    tcMRakU1MYnsl5f5IlWPaluy9VZZP+w2xFLh4QCECdQCxi8KKTpD/yVXTDk7P4W/
    6M33TcNitGrMJcoTWv8Cu7yjFSmbpbFpQUvRfvdCcfvJig6ECmzqc+Vhn1bq8bl3
    c49ms40qHmTb2hogwsMyc59U5LYVuwhYlUMtYbZSlF7sg3v0weA404cuNxfpM1bc
    X/JC+xogeVBBPo+sZSzzq93YWznNmfzgd2oRE9TMfuEmc5eAx3xCTgenbPjqvMB/
    FCHHjdAOM6ks3Fk3UZl92O9ujjQk8LRbR6kDfAm9rn9mYTsdhqOC78FRcJrkVdm+
    qQ==
    -----END CERTIFICATE-----
"""

# PEM 每行前置空白會導致解析失敗，逐行 strip
ROOTCACERT = b"\n".join(line.strip() for line in _ROOT_CA_PEM.splitlines())


class LicenseHelper:
    def __init__(self):
        if sys.platform != "win32":
            raise RuntimeError("LicenseHelper 依賴 Windows NCrypt／TPM，僅支援 Windows")
        self.ncrypt = ctypes.windll.ncrypt
        # 定義一個自定義 OID (例如: 1.3.6.1.4.1.99999.1)
        # 註：1.3.6.1.4.1 是私有企業分支，後面的數字可以自定
        self.CUSTOM_OID = ObjectIdentifier("1.3.6.1.4.1.99999.1")
        self.metadata_json = json.dumps({
            "productId": PRODUCT_ID,
            "version": "1.0.0"
        })

    def __getHWID(self):
        cmd = 'wmic csproduct get uuid'
        uuid = str(subprocess.check_output(cmd, shell=True))
        # Extracting the UUID from the command output
        data = uuid.split(r'\r\n')[1].replace('\\r','')
        return data.strip()
    
    def __getTPMHandle(self):
        h_provider = wintypes.HANDLE()
        self.ncrypt.NCryptOpenStorageProvider(ctypes.byref(h_provider), MS_PLATFORM_CRYPTO_PROVIDER, 0)
        return h_provider
    
    def __encryptedByTPM(self, aes_key):
        key_name=PRODUCT_ID
        h_provider = self.__getTPMHandle()
        h_key = wintypes.HANDLE()

        # 1. DELETE THE OLD KEY (Crucial step)
        status = self.ncrypt.NCryptOpenKey(h_provider, ctypes.byref(h_key), key_name, 0, 0)
        if status == 0:
            self.ncrypt.NCryptDeleteKey(h_key, 0)
        
        # 2. CREATE NEW KEY WITH FULL USAGE
        self.ncrypt.NCryptCreatePersistedKey(h_provider, ctypes.byref(h_key), "RSA", key_name, 0, 0)

        # Set 2048-bit
        bit_length = wintypes.DWORD(2048)
        self.ncrypt.NCryptSetProperty(h_key, "Length", ctypes.byref(bit_length), 4, 0)

        # SET USAGE TO 0xFFFFFFFF (Allow All)
        usage = wintypes.DWORD(0xFFFFFFFF)
        self.ncrypt.NCryptSetProperty(h_key, "Key Usage", ctypes.byref(usage), 4, 0)
        self.ncrypt.NCryptFinalizeKey(h_key, 0)

        # 3. ENCRYPT
        output = (ctypes.c_ubyte * 256)()
        done = wintypes.DWORD()
        self.ncrypt.NCryptEncrypt(h_key, aes_key, len(aes_key), None, output, 256, ctypes.byref(done), 2)
            
        self.ncrypt.NCryptFreeObject(h_key)
        self.ncrypt.NCryptFreeObject(h_provider)

        return bytes(output)[:done.value]   

    def __decryptByTPM(self, encrypted_bytes):
        key_name=PRODUCT_ID
        h_provider = self.__getTPMHandle()
        h_key = wintypes.HANDLE()
        
        # Open with Silent flag
        status = self.ncrypt.NCryptOpenKey(h_provider, ctypes.byref(h_key), key_name, 0, 0x00000040)
        if status != 0:
            raise Exception("Key not found. Did you run generate_license_csr first?")

        output_buffer = (ctypes.c_ubyte * 256)()
        bytes_done = wintypes.DWORD(0)

        # Decrypt with OAEP (Flag 2)
        status = self.ncrypt.NCryptDecrypt(
            h_key, 
            encrypted_bytes, len(encrypted_bytes), 
            None, 
            output_buffer, 
            256, 
            ctypes.byref(bytes_done), 
            2 
        )    
        result = bytes(output_buffer)[:bytes_done.value]
        self.ncrypt.NCryptFreeObject(h_key)
        self.ncrypt.NCryptFreeObject(h_provider)    
        return result 

    ### 這段程式碼的核心目的是：對硬體 ID (HWID) 進行加密，並將其隱藏在數位憑證請求 (CSR) 的欄位中，同時對私鑰進行二次加密。
    ### 用於設備綁定軟體授權，確保金鑰與特定的硬體設備掛鉤。
    def genLicenseCSR(self, csrFilePath: str):
        # 1. 生成隨機的 256-bit AES 金鑰與 128-bit 的初始化向量 (IV)
        aes_key = os.urandom(32)
        iv = os.urandom(16)  

        # 2. Read and encrypted HWID (加密 & BASE64 encoded)
        hwid = self.__getHWID()
        ## 使用 AES-CFB 模式 建立加密器
        encryptor = Cipher(algorithms.AES(aes_key), modes.CFB(iv)).encryptor()
        ## HWID 加密後為 Bytes 型別
        encrypted_hwid_bytes = encryptor.update(hwid.encode()) + encryptor.finalize()
        ## 轉換為 BASE64 String 型別
        encoded_hwid_str = base64.b64encode(encrypted_hwid_bytes).decode('utf-8')

        # 3. 產生 PKI Private/Public Key
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

        # 4. 產生 CSR (CN:BASE64 的加密 HWID)，儲存 CSR 
        csr = x509.CertificateSigningRequestBuilder().subject_name(x509.Name([
                x509.NameAttribute(NameOID.COMMON_NAME, encoded_hwid_str),
            ])).add_extension(
            x509.UnrecognizedExtension(self.CUSTOM_OID, self.metadata_json.encode('utf-8')),
            critical=False
        ).sign(private_key, hashes.SHA256())
        
        csr_pem = csr.public_bytes(serialization.Encoding.PEM)
        with open(csrFilePath, "wb") as f:
            f.write(csr_pem)

        # 5. 取出 Private Key, 再使用 AES 加密 Private Key
        private_pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption()
        )
        encryptor = Cipher(algorithms.AES(aes_key), modes.CFB(iv)).encryptor()
        encryptedPrivateKey = encryptor.update(private_pem) + encryptor.finalize()

        # 6. 在拋棄 AES Key 前, 使用 TPM 將其加密
        encrypedAESKey = self.__encryptedByTPM(aes_key)

        # 7. 本機儲存綁定資料（供 verify／check_license；非 CSR 匯出檔）
        metadata = {
            "hwid": hwid,
            "iv_hex": iv.hex(),
            "encrypedAESKey": encrypedAESKey.hex(),
            "encryptedPrivateKey": encryptedPrivateKey.hex(),
        }
        with open(LICENSE_BINDING_PATH, "w", encoding="utf-8") as f:
            json.dump(metadata, f, indent=4)

    def verifyLicense(self, licenseCert, encryptedAESKey, iv_hex):
        verifyResult = {}
        verifyResult["status"] = False
        # --- 1. Read License & Root CA Cert ---
        licCert = x509.load_pem_x509_certificate(licenseCert)    
        
        rootCAObj = x509.load_pem_x509_certificate(ROOTCACERT)
        rootCAPubKey = rootCAObj.public_key()

        # --- 2. 確認 License 憑證 ---
        # This proves the certificate was not forged and came from your server.
        try:
            rootCAPubKey.verify(
                licCert.signature,
                licCert.tbs_certificate_bytes,
                padding.PKCS1v15(),
                licCert.signature_hash_algorithm,
            )
            logger.debug("✅ Step 1: Certificate Signature Verified (CA Match)")
        except Exception as error:
            logger.error("Error on Step 1: %s", error)
            verifyResult["desc"] = "Verify Fail"
            return verifyResult
        
        # 檢查是否已生效與是否過期
        current_time = datetime.now(timezone.utc)

        # 讀取憑證時間
        not_before = licCert.not_valid_before_utc
        not_after = licCert.not_valid_after_utc

        if current_time < not_before:
            logger.warning("❌ 憑證尚未生效！")
            verifyResult["desc"] = "憑證尚未生效"
            return verifyResult
        elif current_time > not_after:
            logger.warning("❌ 憑證已過期！")
            verifyResult["desc"] = "憑證已過期"
            return verifyResult

        # --- 3. 讀取憑證的 CN (HWID), then BASE64 Decode ---
        common_names = licCert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
        if not common_names:
            logger.warning("❌ Step 2: No Common Name found in certificate.")
            verifyResult["desc"] = "Fail on Common Name"
            return verifyResult
        
        encodedHWID = common_names[0].value
        try:
            encryptedHWIDBytes = base64.b64decode(encodedHWID)
        except Exception as error:
            logger.error("Error on Step 3: %s", error)
            verifyResult["desc"] = "Fail on Common Name"
            return verifyResult

        try:
            extension = licCert.extensions.get_extension_for_oid(self.CUSTOM_OID)
        except ExtensionNotFound:
            logger.warning("憑證缺少自訂 OID 延伸欄位")
            verifyResult["desc"] = "Missing product extension"
            return verifyResult

        # --- 讀取 Meta Data (Product ID and Version)
        raw_json_bytes = extension.value.value
        metadata = json.loads(raw_json_bytes.decode("utf-8"))
        verifyResult["productId"] = metadata["productId"]
        verifyResult["version"] = metadata["version"]
        logger.debug(
            "License productId=%s version=%s",
            verifyResult["productId"],
            verifyResult["version"],
        )
        if metadata.get("productId") != PRODUCT_ID:
            logger.warning("License productId 與本程式 PRODUCT_ID 不符")
            verifyResult["desc"] = "Product ID mismatch"
            return verifyResult

        # --- 4. 以 TPM 解密 AES Key ---
        try:
            encryptedAESKeyBytes = bytes.fromhex(encryptedAESKey)
            AESKey = self.__decryptByTPM(encryptedAESKeyBytes) # Using the function provided earlier
            logger.debug("✅ Step 3: AES Key restored via TPM")
        except Exception as e:
            logger.error(f"Error on Step 4: {e}")
            verifyResult["desc"] = "TPM Decrypt Fail"
            return verifyResult

        # --- 5. 以 AES 解密 HWID ---
        iv = bytes.fromhex(iv_hex)
        decryptor = Cipher(algorithms.AES(AESKey), modes.CFB(iv)).decryptor()
        decryptedHWID = (decryptor.update(encryptedHWIDBytes) + decryptor.finalize()).decode('utf-8')

        # --- 6. Step 5: Final Hardware Validation ---
        actualMachineID = self.__getHWID() # Using your wmic function
        if decryptedHWID == actualMachineID:
            logger.debug(f"✅ Step 4: Hardware Match Success!")
            logger.debug(f"License is VALID for HWID: {decryptedHWID}")
            verifyResult["status"] = True
            return verifyResult
        else:
            logger.warning(f"❌ Step 4: Hardware Mismatch!")
            verifyResult["desc"] = "Hardware Mismatch"
            return verifyResult