from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    app_name: str = "DLFR API"
    database_url: str = "sqlite:///./dlfr.db"
    jwt_secret: str = "DEV_ONLY_CHANGE_THIS_SECRET"
    jwt_expire_minutes: int = 720
    owner_setup_key: str = ""
    cors_origins: str = "http://localhost:5173"
    mandi_api_key: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from_email: str = ""
    report_recipient_email: str = ""
    report_timezone: str = "Asia/Kolkata"
    daily_report_time: str = "20:00"
    monthly_report_day: int = 1
    monthly_report_time: str = "08:00"
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

settings = Settings()
