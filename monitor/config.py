from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MONITOR_", env_file=".env", extra="ignore")
    data_dir: Path = Path("data")
    ai_base_url: str = "https://api.aitunnel.ru/v1"
    ai_key_file: Path | None = None
    ai_model: str = "gpt-4.1-mini"
    vision_model: str = "gpt-4.1"
    ai_enabled: bool = False
    ai_auto_grouping: bool = True
    grouping_batch_size: int = 10
    ai_daily_budget_rub: float = 10
    ai_job_budget_rub: float = 5
    min_balance_rub: float = 20
    ai_balance_path: str = "/aitunnel/balance"
    ai_prompt_price: float = 80
    ai_completion_price: float = 320
    concurrency: int = 3
    domain_delay: float = 2
    max_attempts: int = 3
    browser_enabled: bool = True
    request_timeout: float = 20
    max_page_bytes: int = 3_000_000
    max_agent_steps: int = 2
    cache_ttl_seconds: int = 3600

    def prepare(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        for name in ("evidence", "sessions", "imports", "reports"):
            p = self.data_dir / name
            p.mkdir(exist_ok=True)
            p.chmod(0o700)

    def api_key(self):
        return self.ai_key_file.read_text().strip() if self.ai_key_file else ""
