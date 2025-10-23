#!/usr/bin/env python3
import os, json, time, io, sys
from datetime import datetime, timezone
import boto3
from PIL import Image

REGION      = os.getenv("AWS_REGION", "us-east-1")
S3_BUCKET   = os.environ["S3_BUCKET"]
DDB_AUDIT   = os.environ["DDB_AUDIT"]
DDB_STATUS  = os.environ["DDB_STATUS"]
THUMB_PREFIX= os.getenv("THUMB_PREFIX", "thumb/")
QUEUE_URL   = os.environ["QUEUE_URL"]  # use a URL da fila (não ARN)

sqs = boto3.client("sqs", region_name=REGION)
s3  = boto3.client("s3", region_name=REGION)
ddb = boto3.resource("dynamodb", region_name=REGION)
audit_tb  = ddb.Table(DDB_AUDIT)
status_tb = ddb.Table(DDB_STATUS)
# === Chaves da tabela ProcessingStatus (genérico) ===
STATUS_PK_NAME = os.getenv("STATUS_PK_NAME", "id")   # nome do atributo de partição
STATUS_SK_NAME = os.getenv("STATUS_SK_NAME")         # nome do atributo de sort (opcional)
STATUS_PK_PREFIX = os.getenv("STATUS_PK_PREFIX", "") # prefixo opcional, ex.: "FILE#"
STATUS_SK_VALUE  = os.getenv("STATUS_SK_VALUE")      # valor fixo para SK, ex.: "STATUS"

def _status_key(s3_key: str):
    pk_val = f"{STATUS_PK_PREFIX}{s3_key}" if STATUS_PK_PREFIX else s3_key
    if STATUS_SK_NAME:
        sk_val = STATUS_SK_VALUE if STATUS_SK_VALUE else "STATUS"
        return {STATUS_PK_NAME: pk_val, STATUS_SK_NAME: sk_val}
    else:
        return {STATUS_PK_NAME: pk_val}

def update_status(key: str, status: str, info: dict=None):
    status_tb.update_item(
        Key=_status_key(key),
        UpdateExpression="SET #s=:s, info=:i, updated_at=:t",
        ExpressionAttributeNames={"#s":"status"},
        ExpressionAttributeValues={
            ":s": status,
            ":i": info or {},
            ":t": datetime.now(timezone.utc).isoformat()
        }
    )


def log(msg):
    ts = datetime.now(timezone.utc).isoformat()
    print(f"[{ts}] {msg}", flush=True)

def log_audit(action, data):
    audit_tb.put_item(Item={
        "id": f"{action}#{datetime.now(timezone.utc).isoformat()}",
        "action": action,
        "data": data,
        "ts": int(datetime.now(timezone.utc).timestamp())
    })

def make_thumb(obj_key):
    obj = s3.get_object(Bucket=S3_BUCKET, Key=obj_key)
    body = obj["Body"].read()
    img  = Image.open(io.BytesIO(body))
    img.thumbnail((512, 512))

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

def process_message(payload):
    bucket = payload.get("bucket", S3_BUCKET)
    key    = payload["key"]

    update_status(key, "PROCESSING", {"source":"ec2-worker"})
    out = make_thumb(key)
    update_status(key, "DONE", {"thumb_key": out})
    log_audit("IMAGE_RESIZED", {"key": key, "thumb_key": out})
    log(f"OK: {key} -> {out}")

def main():
    log("Worker iniciado. Lendo SQS...")
    while True:
        resp = sqs.receive_message(
            QueueUrl=QUEUE_URL,
            MaxNumberOfMessages=5,
            WaitTimeSeconds=20,
            VisibilityTimeout=60
        )
        msgs = resp.get("Messages", [])
        if not msgs:
            continue

        for m in msgs:
            receipt = m["ReceiptHandle"]
            body = m["Body"]
            try:
                obj = json.loads(body)
                # Se vier via SNS->SQS, desempacote:
                if "TopicArn" in obj and "Message" in obj:
                    payload = json.loads(obj["Message"])
                else:
                    payload = obj
                process_message(payload)
                sqs.delete_message(QueueUrl=QUEUE_URL, ReceiptHandle=receipt)
            except Exception as e:
                log(f"ERRO: {e}\nBODY: {body}")
                # deixa a mensagem reaparecer (DLQ tratará após n tentativas)

if __name__ == "__main__":
    main()
