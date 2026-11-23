from __future__ import annotations

import os
from dataclasses import dataclass
from fastapi import Header, HTTPException

VALID_ROLES = {"accountant", "chief_accountant", "admin"}


@dataclass
class UserContext:
    user_id: str
    role: str


def get_current_user(
    x_user_id: str = Header(default="accountant-1", alias="X-User-Id"),
    x_user_role: str = Header(default="chief_accountant", alias="X-User-Role"),
) -> UserContext:
    """
    Lightweight RBAC context extractor.
    shortcut: trusts X-User-Role header for local MVP/demo; replace with signed JWT verification before public deployment.
    """
    role = x_user_role.strip().lower()
    if role not in VALID_ROLES:
        raise HTTPException(status_code=403, detail=f"Vai trò '{role}' không hợp lệ. Chỉ chấp nhận: {sorted(VALID_ROLES)}")
    return UserContext(user_id=x_user_id.strip() or "unknown", role=role)


def require_roles(user: UserContext, allowed: set[str], action_name: str) -> None:
    if user.role not in allowed:
        raise HTTPException(
            status_code=403,
            detail=f"Vai trò '{user.role}' không có quyền thực hiện '{action_name}'. Yêu cầu: {sorted(allowed)}.",
        )


def check_approval_permission(user: UserContext, invoice_status: str, grand_total: float) -> None:
    """Enforce Chief Accountant approval for FLAGGED invoices or amounts above the configured threshold."""
    max_accountant_limit = float(os.getenv("STP_MAX_AMOUNT_VND", "20000000"))
    if user.role == "accountant":
        if invoice_status in {"FLAGGED", "VALIDATION_FAILED"}:
            raise HTTPException(
                status_code=403,
                detail="Hoá đơn đang ở trạng thái FLAGGED/VALIDATION_FAILED bắt buộc phải do Kế toán trưởng (chief_accountant) phê duyệt.",
            )
        if grand_total > max_accountant_limit:
            raise HTTPException(
                status_code=403,
                detail=(
                    f"Hoá đơn có giá trị {grand_total:,.0f} VNĐ vượt hạn mức duyệt của Kế toán viên "
                    f"({max_accountant_limit:,.0f} VNĐ). Vui lòng chuyển Kế toán trưởng phê duyệt."
                ),
            )
