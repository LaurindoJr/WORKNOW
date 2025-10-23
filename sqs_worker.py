#!/usr/bin/env python3
import os, json, io
from datetime import datetime, timezone
import boto3
from PIL import Image

# ===== Config via ambiente =====
REGION       = os.getenv("AWS_REGION", "us-east-1")
S3_BUCKET    = os.environ["S3_BUCKET"]
DDB_AUDIT    = os.environ["DDB_AUDIT"]
DDB_STATUS   = os.environ["DDB_STATUS"]
THUMB_PREFIX = os.getenv("THUMB_PREFIX", "thumb/")
QUEUE_URL    = os.environ["QUEUE_URL"]  # URL completa da fila

# ProcessingStatus (apenas PK obrigatória; SK opcional)
STATUS_PK_NAME   = os.getenv("STATUS_PK_NAME", "id")
STATUS_SK_NAME   = os.getenv("STATUS_SK_NAME")
STATUS_PK_PREFIX = os.getenv("STATUS_PK_PREFIX", "")
STATUS_SK_VALUE  = os.getenv("STATUS_SK_VALUE")  # se não setar, usa "STATUS" por padrão

# AuditLogs (suporta PK+SK)
AUDIT_PK_NAME    = os.getenv("AUDIT_PK_NAME", "id")  # ex.: "pk"
AUDIT_SK_NAME    = os.getenv("AUDIT_SK_NAME")        # ex.: "sk"
AUDIT_PK_PREFIX  = os.getenv("AUDIT_PK_PREFIX", "")  # ex.: "AUDIT#"
AUDIT_SK_VALUE   = os.getenv("AUDIT_SK_VALUE")       # se vazio, usa timestamp ISO como SK

# ===== AWS clients =====
sqs = boto3.client("sqs", region_name=REGION)
s3  = boto3.client("s3", region_name=REGION)
ddb = boto3.resource("dynamodb", region_name=REGION)
status_tb = ddb.Table(DDB_STATUS)
audit_tb  = ddb.Table(DDB_AUDIT)

# ===== Helpers =====
def now_iso():
    return datetime.now(timezone.utc).isoformat()

def log(msg):
    print(f"[{now_iso()}] {msg}", flush=True)

# ----- Keys builders -----
def _status_key(s3_key: str):
    pk_val = f"{STATUS_PK_PREFIX}{s3_key}" if STATUS_PK_PREFIX else s3_key
    if STATUS_SK_NAME:
        sk_val = STATUS_SK_VALUE if STATUS_SK_VALUE else "STATUS"
        return {STATUS_PK_NAME: pk_val, STATUS_SK_NAME: sk_val}
    return {STATUS_PK_NAME: pk_val}

def _audit_key(action: str, s3_key: str | None = None):
    base = action if s3_key is None else f"{action}#{s3_key}"
    pk_val = f"{AUDIT_PK_PREFIX}{base}" if AUDIT_PK_PREFIX else base
    if AUDIT_SK_NAME:
        sk_val = AUDIT_SK_VALUE if AUDIT_SK_VALUE else now_iso()
        return {AUDIT_PK_NAME: pk_val, AUDIT_SK_NAME: sk_val}
    return {AUDIT_PK_NAME: pk_val}

# ----- Dynamo helpers -----
def update_status(key: str, status: str, info: dict | None = None):
    status_tb.update_item(
        Key=_status_key(key),
        UpdateExpression="SET #s=:s, info=:i, updated_at=:t",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":s": status,
            ":i": info or {},
            ":t": now_iso(),
        },
    )

def log_audit(action: str, data: dict, s3_key: str | None = None):
    item = _audit_key(action, s3_key)
    item.update({
        "action": action,
        "data": data,
        "ts": int(datetime.now(timezone.utc).timestamp()),
        "at": now_iso(),
    })
    audit_tb.put_item(Item=item)

# ----- Image processing -----
def make_thumb(obj_key: str) -> str:
    obj = s3.get_object(Bucket=S3_BUCKET, Key=obj_key)
    body = obj["Body"].read()
    img  = Image.open(io.BytesIO(body))
    img.thumbnail((512, 512))  # ajuste à necessidade

    buf = io.BytesIO()
    fmt = (img.format or "JPEG").upper()
    if fmt == "PNG":
        img.save(buf, format="PNG", optimize=True)
        out_key = f"{THUMB_PREFIX}{obj_key.rsplit('/',1)[-1].rsplit('.',1)[0]}.png"
        content_type = "image/png"
    else:
        img.save(buf, format="JPEG", quality=85, optimize=True)
        out_key = f"{THUMB_PREFIX}{obj_key.rsplit('/',1)[-1].rsplit('.',1)[0]}.jpg"
        content_type = "image/jpeg"

    buf.seek(0)
    s3.put_object(Bucket=S3_BUCKET, Key=out_key, Body=buf.getvalue(), ContentType=content_type)
    return out_key

# ----- Message processing -----
def process_message(payload: dict):
    bucket = payload.get("bucket", S3_BUCKET)
    key    = payload["key"]

    # status: PROCESSING
    update_status(key, "PROCESSING", {"source": "ec2-worker", "bucket": bucket})

    # generate thumb
    out = make_thumb(key)

    # status: DONE
    update_status(key, "DONE", {"thumb_key": out})
    log_audit("IMAGE_RESIZED", {"key": key, "thumb_key": out}, s3_key=key)
    log(f"OK: {key} -> {out}")

def main():
    log("Worker iniciado. Lendo SQS...")
    while True:
        resp = sqs.receive_message(
            QueueUrl=QUEUE_URL,
            MaxNumberOfMessages=5,
            WaitTimeSeconds=20,
            VisibilityTimeout=60,
        )
        msgs = resp.get("Messages", [])
        if not msgs:
            continue

        for m in msgs:
            receipt = m["ReceiptHandle"]
            body = m["Body"]
            try:
                obj = json.loads(body)
                # Envelope SNS?
                if "TopicArn" in obj and "Message" in obj:
                    payload = json.loads(obj["Message"])
                else:
                    payload = obj

                process_message(payload)
                sqs.delete_message(QueueUrl=QUEUE_URL, ReceiptHandle=receipt)

            except Exception as e:
                # tenta registrar erro sem derrubar o loop
                try:
                    k = None
                    try:
                        k = payload.get("key")  # pode falhar se payload não existir
                    except Exception:
                        pass
                    update_status(k or "<unknown>", "ERROR", {"error": str(e)})
                    log_audit("ERROR_RESIZE", {"error": str(e), "raw": body}, s3_key=k)
                except Exception:
                    pass
                log(f"ERRO: {e}\nBODY: {body}")

if __name__ == "__main__":
    main()
