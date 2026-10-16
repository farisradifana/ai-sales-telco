"""Strict request models (extra=forbid). Money in = decimal string; quantity = integer (PCS or rupiah for SALDO)."""
from datetime import date
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictInt, StringConstraints, model_validator

Channel = Literal['sell_in', 'sell_out']
ScopeType = Literal['outlet', 'district']
# Analytical scopes: regency (kabupaten/kota) and province need a district grant on EVERY kecamatan in the area.
AreaScope = Literal['outlet', 'district', 'regency', 'province']
MoneyStr = Annotated[str, StringConstraints(pattern=r'^\d{1,14}(\.\d{1,4})?$')]
ExternalKey = Annotated[str, StringConstraints(pattern=r'^[A-Za-z0-9][A-Za-z0-9._:-]{5,119}$')]
ConversationKey = Annotated[str, StringConstraints(pattern=r'^[A-Za-z0-9._:-]{8,128}$')]
Sku = Annotated[str, StringConstraints(pattern=r'^[A-Z0-9-]{3,32}$')]
MAX_DAYS = 366


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)


class PeriodPair(Strict):
    """End dates are EXCLUSIVE (WIB calendar days). Both periods must have equal length and not overlap."""
    baseline_start: date | None = None
    baseline_end: date | None = None
    current_start: date | None = None
    current_end: date | None = None

    @model_validator(mode='after')
    def _all_or_none(self):
        vals = [self.baseline_start, self.baseline_end, self.current_start, self.current_end]
        if any(v is not None for v in vals) and not all(v is not None for v in vals):
            raise ValueError('isi keempat tanggal periode, atau kosongkan semuanya untuk default watermark')
        return self

    @property
    def explicit(self) -> bool:
        return self.baseline_start is not None


class DiagnoseRequest(PeriodPair):
    scope: AreaScope
    entity_id: StrictInt = Field(ge=1, le=10_000_000)
    channel: Channel = 'sell_out'


class RankRequest(PeriodPair):
    channel: Channel = 'sell_out'


class StockRequest(Strict):
    account: Literal['self', 'outlet', 'sales'] = 'self'
    outlet_id: StrictInt | None = Field(None, ge=1)
    sales_id: StrictInt | None = Field(None, ge=1)
    product_id: StrictInt | None = Field(None, ge=1)
    as_of: AwareDatetime | None = None


class TargetRequest(Strict):
    target_date: date | None = None
    channel: Channel
    sales_id: StrictInt | None = Field(None, ge=1)  # supervisors only; sales role always resolves to self


class ResolveRequest(Strict):
    query: Annotated[str, StringConstraints(min_length=2, max_length=80)]
    scope: AreaScope | None = None


class DashboardRequest(PeriodPair):
    scope: AreaScope
    entity_id: StrictInt = Field(ge=1)
    channel: Channel


class ContextCreate(Strict):
    evidence_id: UUID
    conversation_key: ConversationKey


class FollowUp(Strict):
    question: Annotated[str, StringConstraints(min_length=1, max_length=500)]
    expected_version: StrictInt = Field(ge=1)


class SaleLine(Strict):
    product_id: StrictInt | None = Field(None, ge=1)
    sku: Sku | None = None
    quantity: StrictInt = Field(ge=1, le=100_000_000)
    unit_price: MoneyStr | None = None
    discount_idr: MoneyStr = '0'

    @model_validator(mode='after')
    def _one_product_ref(self):
        if (self.product_id is None) == (self.sku is None):
            raise ValueError('isi tepat satu: product_id atau sku')
        return self


class DraftRequest(Strict):
    channel: Channel
    outlet_id: StrictInt | None = Field(None, ge=1)
    outlet_code: Annotated[str, StringConstraints(pattern=r'^[A-Z]{2,5}-\d{1,6}$')] | None = None
    lines: list[SaleLine] = Field(min_length=1, max_length=10)
    occurred_at: AwareDatetime | None = None
    customer_token: Annotated[str, StringConstraints(pattern=r'^SYN-[A-Z0-9-]{3,40}$')] | None = None

    @model_validator(mode='after')
    def _outlet_ref(self):
        if (self.outlet_id is None) == (self.outlet_code is None):
            raise ValueError('isi tepat satu: outlet_id atau outlet_code')
        if self.channel == 'sell_in' and self.customer_token:
            raise ValueError('customer_token hanya untuk sell_out')
        return self


class ConfirmRequest(Strict):
    confirmation_event_id: Annotated[str, StringConstraints(min_length=6, max_length=160)]


class DirectSale(Strict):
    external_key: ExternalKey
    outlet_id: StrictInt = Field(ge=1)
    product_id: StrictInt = Field(ge=1)
    quantity: StrictInt = Field(ge=1, le=100_000_000)
    unit_price: MoneyStr
    discount_idr: MoneyStr = '0'
    occurred_at: AwareDatetime | None = None  # omitted -> DB clock after locks (recommended)
    customer_token: Annotated[str, StringConstraints(pattern=r'^SYN-[A-Z0-9-]{3,40}$')] | None = None
    client_confirmation_id: Annotated[str, StringConstraints(min_length=6, max_length=160)]


class ReversalRequest(Strict):
    channel: Channel
    transaction_id: StrictInt = Field(ge=1)
    reason: Annotated[str, StringConstraints(min_length=5, max_length=300)]
    replacement: DraftRequest | None = None


class TransferRequest(Strict):
    to_sales_id: StrictInt = Field(ge=1)
    product_id: StrictInt = Field(ge=1)
    quantity: StrictInt = Field(ge=1, le=100_000_000)
    reason: Annotated[str, StringConstraints(min_length=5, max_length=300)]


class CorrectionRequest(Strict):
    channel: Channel
    external_key: ExternalKey
    outlet_id: StrictInt = Field(ge=1)
    product_id: StrictInt = Field(ge=1)
    quantity: StrictInt = Field(ge=1, le=100_000_000)
    unit_price: MoneyStr
    discount_idr: MoneyStr = '0'
    occurred_at: AwareDatetime
    customer_token: Annotated[str, StringConstraints(pattern=r'^SYN-[A-Z0-9-]{3,40}$')] | None = None
    reason: Annotated[str, StringConstraints(min_length=10, max_length=300)]


class ActionDecision(Strict):
    decision: Literal['approve', 'reject']
    note: Annotated[str, StringConstraints(max_length=300)] = ''


class NotificationPrefIn(Strict):
    target_channel: Channel
    daily_target_opt_in: bool
    send_hour_wib: StrictInt = Field(16, ge=7, le=20)
    workdays: list[StrictInt] = Field(default_factory=lambda: [1, 2, 3, 4, 5], min_length=1, max_length=7)


class DevLogin(Strict):
    principal_id: Annotated[str, StringConstraints(pattern=r'^[a-z0-9][a-z0-9._:-]{2,63}$')]


class OutboxDispatch(Strict):
    limit: StrictInt = Field(20, ge=1, le=100)
    lease_seconds: StrictInt = Field(60, ge=10, le=600)
    worker_id: Annotated[str, StringConstraints(pattern=r'^[A-Za-z0-9._:-]{1,64}$')] = 'n8n'


class NudgeRun(Strict):
    now: AwareDatetime | None = None  # test hook; production uses server clock


class ToolCallEnvelope(Strict):
    """MCP bridge -> API. Ticket binds principal server-side; arguments validated per tool."""
    arguments: dict


def dec_or_zero(v: str | None) -> Decimal:
    return Decimal(v) if v is not None else Decimal(0)
