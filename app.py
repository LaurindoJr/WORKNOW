import os, json, uuid
from datetime import date
from typing import Optional

from flask import Flask, request, render_template, redirect, url_for, flash
import psycopg2
import psycopg2.extras
import boto3
from botocore.exceptions import BotoCoreError, ClientError

# Configurações
DB = {
    "host": os.environ["RDS_HOST"],
    "dbname": os.environ["RDS_DB"],
    "user": os.environ["RDS_USER"],
    "password": os.environ["RDS_PASS"],
    "port": 5432,
}

AWS_REGION   = os.getenv("AWS_REGION", "us-east-1")
S3_BUCKET    = os.environ["S3_BUCKET"]
DDB_AUDIT    = os.getenv("DDB_AUDIT", "kcl-AuditLogs")
QUEUE_URL    = os.environ["QUEUE_URL"]

s3       = boto3.client("s3", region_name=AWS_REGION)
sqs      = boto3.client("sqs", region_name=AWS_REGION)
dynamo   = boto3.resource("dynamodb", region_name=AWS_REGION)
audit_tbl = dynamo.Table(DDB_AUDIT)

def db_conn():
    return psycopg2.connect(
        cursor_factory=psycopg2.extras.RealDictCursor, **DB
    )

def log_audit(action: str, data: dict):
    """
    Auditoria em kcl-AuditLogs (PK+SK).
      pk = "APP#<ACTION>"
      sk = uuid4()
    """
    audit_tbl.put_item(Item={
        "pk": f"APP#{action}",
        "sk": str(uuid.uuid4()),
        "ts": date.today().isoformat(),
        "data": data,
    })

def s3_presigned_url(bucket: str, key: str, minutes: int = 60) -> str:
    return s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket, "Key": key},
        ExpiresIn=minutes * 60,
    )

def thumb_candidate_keys(image_key: Optional[str]):
    """Para uma imagem original, retorna candidatos de thumb (.jpg e .png)."""
    if not image_key:
        return []
    base = image_key.split("/")[-1]
    name_noext = base.rsplit(".", 1)[0]
    return [f"thumb/{name_noext}.jpg", f"thumb/{name_noext}.png"]

def enqueue_image(s3_key: str, book_id: Optional[int] = None):
    """
    Publica mensagem na SQS para o worker processar a imagem.
    """
    payload = {"bucket": S3_BUCKET, "key": s3_key}
    if book_id is not None:
        payload["book_id"] = str(book_id)

    try:
        sqs.send_message(QueueUrl=QUEUE_URL, MessageBody=json.dumps(payload))
    except (BotoCoreError, ClientError) as e:
        log_audit("SQS_PUBLISH_ERROR", {"error": str(e), "payload": payload})

# Aplicação
app = Flask(__name__)
app.secret_key = os.urandom(16)

@app.route("/health")
def health():
    return {"ok": True}, 200

# Home / Lista
@app.route("/")
def index():
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM books ORDER BY id DESC")
        books = cur.fetchall()

    # id -> URL (assinada) da thumb, se existir
    thumbs = {}

    for b in books:
        for tkey in thumb_candidate_keys(b.get("image_key")):
            try:
                s3.head_object(Bucket=S3_BUCKET, Key=tkey)
                thumbs[b["id"]] = s3_presigned_url(S3_BUCKET, tkey, 60)
                break
            except s3.exceptions.ClientError:
                continue

    return render_template("index.html", books=books, thumbs=thumbs)

# Form de novo livro
@app.route("/books/new")
def new_book():
    return render_template("book_form.html", book=None)

# Create novo livro
@app.route("/books", methods=["POST"])
def create_book():
    code    = request.form["code"].strip()
    title   = request.form["title"].strip()
    author  = request.form["author"].strip()
    summary = request.form.get("summary", "").strip()

    image_key = None
    file = request.files.get("image")
    if file and file.filename:
        image_key = f"uploads/{uuid.uuid4().hex}_{file.filename.replace(' ', '_')}"
        s3.upload_fileobj(file, S3_BUCKET, image_key, ExtraArgs={"ContentType": file.mimetype})

    with db_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO books (code,title,author,summary,image_key) "
            "VALUES (%s,%s,%s,%s,%s) RETURNING id",
            (code, title, author, summary, image_key),
        )
        book_id = cur.fetchone()["id"]

    if image_key:
        enqueue_image(image_key, book_id=book_id)

    log_audit("CREATE", {"book_id": book_id, "code": code})
    flash("Livro criado!", "success")
    return redirect(url_for("index"))

# Mostrar Livro
@app.route("/books/<int:book_id>")
def show_book(book_id):
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM books WHERE id=%s", (book_id,))
        book = cur.fetchone()
        cur.execute("SELECT * FROM rentals WHERE book_id=%s ORDER BY id DESC", (book_id,))
        rentals = cur.fetchall()

    thumb_url = None
    if book and book.get("image_key"):
        for tkey in thumb_candidate_keys(book["image_key"]):
            try:
                s3.head_object(Bucket=S3_BUCKET, Key=tkey)
                thumb_url = s3_presigned_url(S3_BUCKET, tkey, 60)
                break
            except s3.exceptions.ClientError:
                continue

    return render_template("book_detail.html", book=book, rentals=rentals, thumb_url=thumb_url)

# Edit
@app.route("/books/<int:book_id>/edit")
def edit_book(book_id):
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM books WHERE id=%s", (book_id,))
        book = cur.fetchone()
    return render_template("book_form.html", book=book)

# Update
@app.route("/books/<int:book_id>", methods=["POST"])
def update_book(book_id):
    title   = request.form["title"].strip()
    author  = request.form["author"].strip()
    summary = request.form.get("summary", "").strip()

    new_key = None
    file = request.files.get("image")
    if file and file.filename:
        new_key = f"uploads/{uuid.uuid4().hex}_{file.filename.replace(' ', '_')}"
        s3.upload_fileobj(file, S3_BUCKET, new_key, ExtraArgs={"ContentType": file.mimetype})

    with db_conn() as conn, conn.cursor() as cur:
        if new_key:
            cur.execute(
                "UPDATE books SET title=%s,author=%s,summary=%s,image_key=%s WHERE id=%s",
                (title, author, summary, new_key, book_id)
            )
        else:
            cur.execute(
                "UPDATE books SET title=%s,author=%s,summary=%s WHERE id=%s",
                (title, author, summary, book_id)
            )

    if new_key:
        enqueue_image(new_key, book_id=book_id)

    log_audit("UPDATE", {"book_id": book_id})
    flash("Livro atualizado!", "success")
    return redirect(url_for("show_book", book_id=book_id))

# Delete
@app.route("/books/<int:book_id>/delete", methods=["POST"])
def delete_book(book_id):
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM books WHERE id=%s", (book_id,))
    log_audit("DELETE", {"book_id": book_id})
    flash("Livro removido.", "info")
    return redirect(url_for("index"))

# Aluguéis
@app.route("/rentals/<int:book_id>/new", methods=["POST"])
def rent_book(book_id):
    renter = request.form["renter"].strip()
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO rentals (book_id,renter,start_date,status) "
            "VALUES (%s,%s,%s,'OPEN')",
            (book_id, renter, date.today())
        )
    log_audit("RENT", {"book_id": book_id, "renter": renter})
    flash("Aluguel criado!", "success")
    return redirect(url_for("show_book", book_id=book_id))

@app.route("/rentals/<int:rental_id>/return", methods=["POST"])
def return_rental(rental_id):
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE rentals SET end_date=%s,status='CLOSED' WHERE id=%s",
            (date.today(), rental_id)
        )
        cur.execute("SELECT book_id FROM rentals WHERE id=%s", (rental_id,))
        row = cur.fetchone()
        book_id = row["book_id"] if row else None
    log_audit("RETURN", {"rental_id": rental_id})
    flash("Devolução registrada.", "info")
    return redirect(url_for("show_book", book_id=book_id))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
