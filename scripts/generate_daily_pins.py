from __future__ import annotations

from datetime import datetime
from sqlalchemy import select, func

from app.database import SessionLocal
from app.models import Product, PinCreative, PinGenerationJob
from app.models.core import PinCreativeType
from app.services.ai_content import AIContentService, AIContentError


DAILY_PIN_TARGET = 15

CREATIVE_TYPES = [
    PinCreativeType.PRODUCT_FOCUS,
    PinCreativeType.LIFESTYLE,
    PinCreativeType.PROBLEM_SOLUTION,
    PinCreativeType.GIFT_IDEA,
    PinCreativeType.MINIMALIST,
]


def main() -> None:
    db = SessionLocal()

    try:
        print("=" * 60)
        print("PinPilot - Daily Pinterest Pin Generator")
        print("=" * 60)
        print(f"Started: {datetime.now():%Y-%m-%d %H:%M:%S}")
        print(f"Daily target: {DAILY_PIN_TARGET}")
        print()

        products = db.scalars(
            select(Product).order_by(Product.id)
        ).all()

        if not products:
            print("ERROR: No products found.")
            return

        # Ürünleri mevcut kreatif sayılarına göre sırala.
        # Böylece daha az işlenmiş ürünler öncelik kazanır.
        creative_counts = dict(
            db.execute(
                select(
                    PinCreative.product_id,
                    func.count(PinCreative.id),
                ).group_by(PinCreative.product_id)
            ).all()
        )

        products = sorted(
            products,
            key=lambda product: (
                creative_counts.get(product.id, 0),
                product.id,
            ),
        )

        service = AIContentService(db)

        total_created = 0
        product_index = 0

        while total_created < DAILY_PIN_TARGET and product_index < len(products):
            product = products[product_index]
            creative_type = CREATIVE_TYPES[
                total_created % len(CREATIVE_TYPES)
            ]

            print(
                f"[{total_created + 1}/{DAILY_PIN_TARGET}] "
                f"Product #{product.id}: {product.title[:70]}"
            )
            print(f"Creative type: {creative_type.value}")

            job = PinGenerationJob(
                product_id=product.id,
                status="running",
            )

            db.add(job)
            db.commit()
            db.refresh(job)

            try:
                created = service.generate(
                    product=product,
                    creative_type=creative_type,
                    desired_count=1,
                )

                if created:
                    total_created += len(created)

                    print(
                        f"SUCCESS: {len(created)} creative generated."
                    )

                    for creative in created:
                        print(f"  ID: {creative.id}")
                        print(f"  Title: {creative.title}")
                        print(f"  Image: {creative.image_path}")
                        print(f"  URL: {creative.destination_url}")

                    job.status = "completed"
                    job.completed_at = datetime.utcnow()
                    db.commit()

                else:
                    print(
                        "SKIPPED: This product/type combination "
                        "already exists."
                    )

                    job.status = "completed"
                    job.completed_at = datetime.utcnow()
                    db.commit()

            except AIContentError as exc:
                print(f"ERROR: {exc}")

                job.status = "failed"
                job.error_message = str(exc)
                job.completed_at = datetime.utcnow()
                db.commit()

            except Exception as exc:
                print(f"UNEXPECTED ERROR: {exc}")

                job.status = "failed"
                job.error_message = str(exc)
                job.completed_at = datetime.utcnow()
                db.commit()

            print()
            product_index += 1

        print("=" * 60)
        print("DAILY GENERATION FINISHED")
        print("=" * 60)
        print(f"Created: {total_created}/{DAILY_PIN_TARGET}")
        print(f"Finished: {datetime.now():%Y-%m-%d %H:%M:%S}")
        print("=" * 60)

    finally:
        db.close()


if __name__ == "__main__":
    main()