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

    # SharePoint document library watcher
    # Sharing link URL for the folder (e.g. from Share → Copy link in OneDrive/SharePoint).
    # When set, children are listed via the Graph Shares API which supports cross-tenant access.
    sharepoint_url: str = ""
    # Drive ID of the SharePoint library to monitor.
    # Find it via GET /me/drive/sharedWithMe -> remoteItem.parentReference.driveId
    sharepoint_drive_id: str = ""
    # Option A: subfolder path within the drive root (e.g. "CV Repository")
    sharepoint_folder_path: str = ""
    # Option B: item ID of a specific shared folder (remoteItem.id from /me/drive/sharedWithMe).
    # When set, takes precedence over sharepoint_folder_path.
    sharepoint_folder_item_id: str = ""
    # How often (in seconds) to check the SharePoint library for new files
    sharepoint_poll_interval: int = 3600
    # Password for password-protected sharing links (set via X-Sharing-Link-Password header)
    sharepoint_password: str = ""

    # Webhook / public URL settings
    webhook_base_url: str = ""
    webhook_secret: str = ""

    # Secure file download endpoint
    file_download_secret: str = ""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


settings = Settings()
