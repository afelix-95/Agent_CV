"""Language detection for ingested documents."""
from langdetect import detect, LangDetectException


def detect_language(text: str) -> str:
    """
    Detect the language of a text sample using langdetect.
    Returns ISO 639-1 language code (e.g., 'en', 'pt', 'es').
    Defaults to 'en' if detection fails or text is too short.
    """
    if not text or len(text.strip()) < 50:
        return "en"  # Default to English for very short text

    try:
        # Take a sample of up to 5000 chars to avoid API overhead
        sample = text[:5000]
        lang = detect(sample)
        # Normalize: langdetect may return codes like 'pt-BR', standardize to base code
        return lang.split("-")[0].lower()
    except LangDetectException:
        return "en"  # Default to English on detection failure
    except Exception:
        return "en"  # Fallback to English for any other error
