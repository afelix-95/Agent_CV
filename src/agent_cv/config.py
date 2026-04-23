from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_env: str = "dev"
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    log_level: str = "INFO"

    postgres_dsn: str
    pdf_root: str = "PDFs/REPOCV"

    azure_openai_endpoint: str = ""
    azure_openai_api_key: str = ""
    azure_openai_api_version: str = "2024-10-21"
    azure_openai_chat_deployment: str = ""
    azure_openai_embedding_deployment: str = ""

    teams_bot_app_id: str = ""
    teams_bot_app_password: str = ""
    teams_bot_tenant_id: str = ""

    # Microsoft Graph delegated access (ROPC flow via bot service account)
    graph_user_email: str = ""
    graph_user_password: str = ""
    # Space-separated delegated scopes; defaults to all admin-consented permissions
    graph_scopes: str = "https://graph.microsoft.com/.default"
    # How often (in seconds) the polling bot checks for new messages
    graph_poll_interval: int = 5

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


settings = Settings()
