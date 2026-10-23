from fastapi import APIRouter, Depends

from app.api.deps import get_db
from app.core.db import Database
from app.schemas.product import ProductOut
from app.services import products as product_service

router = APIRouter(prefix="/products", tags=["products"])


@router.get("", response_model=list[ProductOut])
def list_products(db: Database = Depends(get_db)):
    return product_service.list_products(db)
