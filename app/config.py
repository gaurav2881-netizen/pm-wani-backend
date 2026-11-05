"""Settings, read from environment variables or /etc/pmwani-api.env."""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file="/etc/pmwani-api.env", extra="ignore")

    # Core
    database_url: str = "postgresql+psycopg://pmwani:pmwani@localhost:5432/pmwani"
    jwt_secret: str
    jwt_days: int = 30
    portal_name: str = "PM-WANI Free Wi-Fi"
    public_base_url: str = "http://3.7.159.119"
    cors_origins: str = "*"

    # OTP
    otp_demo: bool = True                 # True: OTP returned in API response / shown on page, no SMS sent
    otp_ttl_seconds: int = 300
    otp_max_attempts: int = 5
    otp_max_per_10min: int = 5
    sms_provider: str = "demo"            # demo | msg91
    msg91_authkey: str = ""
    msg91_template_id: str = ""           # DLT-approved MSG91 template; must contain ##otp##

    # Roles
    pdoa_phones: str = ""                 # comma separated 10-digit numbers that become PDOA admins on login

    # Hotspot / RADIUS
    chilli_uamsecret: str = ""
    interim_interval: int = 300
    max_session_seconds: int = 4 * 3600   # a single Wi-Fi session never lasts longer than this
    hotspot_online_minutes: int = 15      # AP counts as online if it sent accounting this recently
    free_login_on_portal: bool = True     # portal (no app) auto-claims the free plan if the user has none

    # Payments (Razorpay). Empty key id => mock mode for development.
    razorpay_key_id: str = ""
    razorpay_key_secret: str = ""
    razorpay_webhook_secret: str = ""

    @property
    def payments_mock(self) -> bool:
        return not (self.razorpay_key_id and self.razorpay_key_secret)

    @property
    def pdoa_phone_set(self) -> set[str]:
        return {p.strip()[-10:] for p in self.pdoa_phones.split(",") if p.strip()}


@lru_cache
def get_settings() -> Settings:
    return Settings()
