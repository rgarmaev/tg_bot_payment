from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import String, Integer, BigInteger, DateTime, Boolean, ForeignKey, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tg_user_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    orders: Mapped[list[Order]] = relationship("Order", back_populates="user")  # type: ignore  # noqa: F821
    subscriptions: Mapped[list[Subscription]] = relationship("Subscription", back_populates="user")  # type: ignore  # noqa: F821


class OrderStatus:
    NEW = "new"
    PENDING = "pending"
    PAID = "paid"
    CANCELED = "canceled"
    EXPIRED = "expired"


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    amount: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(8), default="RUB")
    status: Mapped[str] = mapped_column(String(16), default=OrderStatus.NEW, index=True)
    external_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    payment_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    user: Mapped[User] = relationship("User", back_populates="orders")


class Subscription(Base):
    __tablename__ = "subscriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    inbound_id: Mapped[int] = mapped_column(Integer, default=1)
    xray_uuid: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    config_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    config_qr_png_b64: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    user: Mapped[User] = relationship("User", back_populates="subscriptions")
