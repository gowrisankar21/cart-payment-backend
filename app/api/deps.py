from fastapi import Request

from app.core.config import Settings
from app.core.db import Database


def get_db(request: Request) -> Database:
    return request.app.state.db


def get_settings(request: Request) -> Settings:
    return request.app.state.settings
