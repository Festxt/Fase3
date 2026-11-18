"""Background worker to consume invoice batches from RabbitMQ and write to MySQL."""

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import logging
import os
import time

import pika
from pika.exceptions import AMQPConnectionError
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, sessionmaker

from models import Invoice, InvoiceItem

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

DATABASE_URL = os.environ["CENTRAL_DATABASE_URL"]
RABBITMQ_URL = os.environ["RABBITMQ_URL"]
INGESTION_QUEUE = os.environ["INGESTION_QUEUE"]

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine)


def parse_sold_at(raw_value: str) -> datetime:
    """Parse ISO-8601 datetime string into a naive UTC datetime for MySQL DATETIME."""
    dt = datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def persist_batch(session: Session, payload: dict) -> tuple[int, int]:
    """Insert each invoice idempotently. Returns (accepted_count, duplicate_count)."""
    store_id = str(payload["store_id"]).strip()
    if not store_id or len(store_id) > 20:
        raise ValueError(f"Invalid store_id: {store_id!r}")

    invoices_data = payload["invoices"]
    if not isinstance(invoices_data, list):
        raise ValueError("'invoices' must be a list")

    accepted = 0
    duplicates = 0
    received_at = datetime.now(timezone.utc).replace(tzinfo=None)

    for inv_data in invoices_data:
        store_invoice_id = int(inv_data["store_invoice_id"])
        register_id = str(inv_data["register_id"]).strip()
        sold_at = parse_sold_at(str(inv_data["sold_at"]))
        total = Decimal(str(inv_data["total"]))

        if not register_id or len(register_id) > 40 or total < 0:
            raise ValueError(f"Invalid invoice header for store_invoice_id={store_invoice_id}")

        items_objs: list[InvoiceItem] = []
        for item_data in inv_data["items"]:
            ean = str(item_data["ean"]).strip()
            product_name = str(item_data["product_name"]).strip()
            quantity = int(item_data["quantity"])
            unit_price = Decimal(str(item_data["unit_price"]))
            subtotal = Decimal(str(item_data["subtotal"]))

            if (
                not ean
                or len(ean) > 13
                or not product_name
                or len(product_name) > 120
                or quantity <= 0
                or unit_price < 0
                or subtotal < 0
            ):
                raise ValueError(f"Invalid item data in invoice {store_invoice_id}")

            items_objs.append(
                InvoiceItem(
                    ean=ean,
                    product_name=product_name,
                    quantity=quantity,
                    unit_price=unit_price,
                    subtotal=subtotal,
                )
            )

        invoice = Invoice(
            store_id=store_id,
            store_invoice_id=store_invoice_id,
            register_id=register_id,
            sold_at=sold_at,
            received_at=received_at,
            total=total,
            items=items_objs,
        )

        try:
            with session.begin_nested():
                session.add(invoice)
                session.flush()
            accepted += 1
        except IntegrityError as exc:
            if "uq_invoices_store_invoice" in str(exc.orig) or "Duplicate entry" in str(exc.orig):
                duplicates += 1
            else:
                raise

    session.commit()
    return accepted, duplicates


def on_message(ch, method, properties, body: bytes) -> None:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        logger.error("Malformed JSON message discarded: %s", exc)
        ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
        return

    session = SessionLocal()
    try:
        accepted, duplicates = persist_batch(session, payload)
        ch.basic_ack(delivery_tag=method.delivery_tag)
        logger.info(
            "Batch from %s processed: %d accepted, %d duplicates",
            payload.get("store_id"),
            accepted,
            duplicates,
        )
    except (KeyError, ValueError, TypeError, InvalidOperation, IntegrityError) as exc:
        session.rollback()
        logger.error("Invalid payload discarded (will not requeue): %s", exc)
        ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
    except OperationalError as exc:
        session.rollback()
        logger.warning("Database connection error, requeuing message: %s", exc)
        time.sleep(2)
        ch.basic_nack(delivery_tag=method.delivery_tag, requeue=True)
    except Exception as exc:
        session.rollback()
        logger.exception("Unexpected error processing batch, requeuing: %s", exc)
        time.sleep(2)
        ch.basic_nack(delivery_tag=method.delivery_tag, requeue=True)
    finally:
        session.close()


def main() -> None:
    parameters = pika.URLParameters(RABBITMQ_URL)

    while True:
        try:
            logger.info("Connecting to RabbitMQ...")
            connection = pika.BlockingConnection(parameters)
            channel = connection.channel()

            channel.queue_declare(queue=INGESTION_QUEUE, durable=True)
            channel.basic_qos(prefetch_count=1)
            channel.basic_consume(
                queue=INGESTION_QUEUE,
                on_message_callback=on_message,
                auto_ack=False,
            )

            logger.info("Worker ready. Listening on queue '%s'...", INGESTION_QUEUE)
            channel.start_consuming()
        except AMQPConnectionError as exc:
            logger.warning("RabbitMQ unavailable (%s). Retrying in 5s...", exc)
            time.sleep(5)
        except KeyboardInterrupt:
            logger.info("Shutting down worker...")
            break


if __name__ == "__main__":
    main()
