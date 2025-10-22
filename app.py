import os, json, uuid
from datetime import date
from flask import Flask, request, render_template, redirect, url_for, flash
import psycopg2, psycopg2.extras
import boto3

from typing import Optional


# ---------- Config ----------
DB = {
    "host": os.environ["RDS_HOST"],
    "dbname": os.environ["RDS_DB"],
    "user": os.environ["RDS_USER"],
    "password": os.environ["RDS_PASS"],
    "port": 5432,
}
AWS_REGION    = os.getenv("AWS_REGION", "us-east-1")
S3_BUCKET     = os.environ["S3_BUCKET"]
SNS_TOPIC_ARN = os.environ["SNS_TOPIC_ARN"]
DDB_AUDIT     = os.getenv("DDB_AUDIT", "kcl-AuditLogs")

s3     = boto3.client("s3", region_name=AWS_REGION)
sns    = boto3.client("sns", region_name=AWS_REGION)
dynamo = boto3.resource("dynamodb", region_name=AWS_REGION)
audit_tbl = dynamo.Table(DDB_AUDIT)

def db_conn():
    return psycopg2.connect(cursor_factory=psycopg2.extras.RealDictCursor, **DB)

def log_audit(action, data):
    audit_tbl.put_item(Item={
        "pk": f"APP#{action}",
        "sk": str(uuid.uuid4()),
        "data": data
    })

def thumb_from(image_key: Optional[str]) -> Optional[str]:
    """Dado o key original no S3, retorna o key esperado da thumbnail (ou None)."""
    if not image_key:
        return None
    base = image_key.split("/")[-1]
    name_noext = base.rsplit(".", 1)[0]
    return f"thumbs/{name_noext}.jpg"

# ---------- App ----------
app = Flask(__name__)
app.secret_key = os.urandom(16)

@app.route("/")
def index():
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM books ORDER BY id DESC")
        books = cur.fetchall()

    # mapa id -> thumb_key (se existir imagem original)
    thumbs = {b["id"]: thumb_from(b.get("image_key")) for b in books}
    return render_template("index.html", books=books, thumbs=thumbs, bucket=S3_BUCKET)

@app.route("/books/new")
def new_book():
    return render_template("book_form.html", book=None)

@app.route("/books", methods=["POST"])
def create_book():
    code    = request.form["code"].strip()
    title   = request.form["title"].strip()
    author  = request.form["author"].strip()
    summary = request.form.get("summary","").strip()

    image_key = None
    file = request.files.get("image")
    if file and file.filename:
        image_key = f"uploads/{uuid.uuid4().hex}_{file.filename.replace(' ', '_')}"
        s3.upload_fileobj(file, S3_BUCKET, image_key, ExtraArgs={"ContentType": file.mimetype})

    with db_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO books (code,title,author,summary,image_key) VALUES (%s,%s,%s,%s,%s) RETURNING id",
            (code, title, author, summary, image_key),
        )
        book_id = cur.fetchone()["id"]

    if image_key:
        # publica no SNS com o payload que a Lambda espera (bucket + key)
        sns.publish(
            TopicArn=SNS_TOPIC_ARN,
            Message=json.dumps({
                "book_id": str(book_id),
                "bucket": S3_BUCKET,
                "key": image_key
            })
        )

    log_audit("CREATE", {"book_id": book_id, "code": code})
    flash("Livro criado!", "success")
    return redirect(url_for("index"))

@app.route("/books/<int:book_id>")
def show_book(book_id):
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM books WHERE id=%s", (book_id,))
        book = cur.fetchone()
        cur.execute("SELECT * FROM rentals WHERE book_id=%s ORDER BY id DESC", (book_id,))
        rentals = cur.fetchall()

    thumb_key = thumb_from(book["image_key"]) if book and book.get("image_key") else None
    return render_template("book_detail.html", book=book, rentals=rentals, thumb_key=thumb_key, bucket=S3_CART_BUCKET if False else S3_BUCKET)

@app.route("/books/<int:book_id>/edit")
def edit_book(book_id):
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM books WHERE id=%s", (book_id,))
        book = cur.fetchone()
    return render_template("book_form.html", book=book)

@app.route("/books/<int:book_id>", methods=["POST"])
def update_book(book_id):
    title   = request.form["title"].strip()
    author  = request.form["author"].strip()
    summary = request.form.get("summary","").strip()

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
        sns.publish(
            TopicArn=SNS_TOPIC_ARN,
            Message=json.dumps({
                "book_id": str(book_id),
                "bucket": S3_BUCKET,
                "key": new_key
            })
        )

    log_audit("UPDATE", {"book_id": book_id})
    flash("Livro atualizado!", "success")
    return redirect(url_for("show_book", book_id=book_id))

@app.route("/books/<int:book_id>/delete", methods=["POST"])
def delete_book(book_id):
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM books WHERE id=%s", (book_id,))
    log_audit("DELETE", {"book_id": book_id})
    flash("Livro removido.", "info")
    return redirect(url_for("index"))

# --- Aluguéis ---
@app.route("/rentals/<int:book_id>/new", methods=["POST"])
def rent_book(book_id):
    renter = request.form["renter"].strip()
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO rentals (book_id,renter,start_date,status) VALUES (%s,%s,%s,'OPEN')",
            (book_id, renter, date.today())
        )
    log_audit("RENT", {"book_id": book_id, "renter": renter})
    flash("Aluguel criado!", "success")
    return redirect(url_for("show_book", book_id=book_id))

@app.route("/rentals/<int:rental_id>/return", methods=["POST"])
def return_rental(rental_id):
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute("UPDATE rentals SET end_date=%s,status='CLOSED' WHERE id=%s",
                    (date.today(), rental_id))
        cur.execute("SELECT book_id FROM rentals WHERE id=%s", (rental_id,))
        row = cur.fetchone()
        book_id = row["book_id"] if row else None
    log_audit("RETURN", {"rental_id": rental_id})
    flash("Devolução registrada.", "info")
    return redirect(url_for("show_book", book_id=book_id))

if __name__ == "__main__":
    # para testes locais; em produção use gunicorn/uwsgi
    app.run(host="0.0.0.0", port=5000)
