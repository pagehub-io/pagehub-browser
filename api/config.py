from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    env: str = "development"
    service_name: str = "pagehub-browser"
    git_commit: str = "unknown"

    max_sessions: int = 20
    idle_timeout_seconds: int = 300
    idle_timeout_max_seconds: int = 900
    reaper_interval_seconds: int = 30
    tombstone_max: int = 256

    max_log_entries: int = 500

    request_body_max_bytes: int = 1048576

    # Dev-only SSRF escape hatch: when ENV is exactly "development" AND this is true, the
    # navigate deny-list permits loopback / RFC1918 / link-local hosts so `make up` can drive
    # a LOCAL target frontend (localhost / host.docker.internal / the docker bridge). Ignored
    # in every other env (deny-by-default — staging/qa/prod keep the strict deny-list).
    browser_allow_private_hosts: bool = False

    default_viewport_width: int = 1280
    default_viewport_height: int = 720
    headless: bool = True

    action_timeout_max_ms: int = 110000
    modal_function_timeout_ms: int = 120000
    lock_acquire_timeout_ms: int = 5000

    sentry_dsn: str | None = None

    # Bearer token gating /v1/admin/* endpoints (currently: reset-sessions). Fail-closed:
    # when unset, every admin request is 401. Set this on any deploy that needs an
    # operator escape hatch for state purges.
    admin_auth_token: str | None = None

    port: int = 4010
    log_level: str = "INFO"

    engine: str = "playwright"


settings = Settings()
