from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from functools import lru_cache
from threading import Lock
from typing import Any

import httpx
from openai import AzureOpenAI

from agent_cv.config import settings
from agent_cv.db.connection import get_connection
from agent_cv.services.query_service import QueryAnalysis, normalize_text

logger = logging.getLogger(__name__)

MAX_AGENT_ITERATIONS = 15

# ------------------------------------------------------------------ #
# Tool schema                                                          #
# ------------------------------------------------------------------ #

TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "search_certifications",
            "description": (
                "Search the employee certifications database by technology, vendor, or keyword. "
                "Returns employees with matching certifications including name, vendor, status, and dates. "
                "Use this whenever the user asks who has certifications in a specific area. "
                "Broad competency keywords (e.g. 'storage', 'security', 'networking', 'cloud', 'virtualization') "
                "are automatically expanded server-side to include related vendors and technologies — "
                "a single call with the competency keyword is sufficient for certifications. "
                "Supports pagination: use 'offset' to fetch the next page of results (page size is 15)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Technology, vendor, or certification name (e.g. 'Azure', 'Red Hat', 'CCNA', 'AZ-900')",
                    },
                    "employee_name": {
                        "type": "string",
                        "description": "Optional: restrict to a specific employee",
                    },
                    "status": {
                        "type": "string",
                        "enum": ["active", "expired", "any"],
                        "description": "Filter by certificate status. Default: 'any'.",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Number of results to skip for pagination. Default: 0 (first page). Use 15 for the second page, 30 for the third, etc.",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_experience",
            "description": (
                "Search employee CV documents for work experience, skills, and professional background. "
                "Returns relevant excerpts. Use this for experience/skill queries, "
                "or to complement a certification search with evidence from CVs."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Skill, technology, or domain (e.g. 'cybersecurity', 'cloud infrastructure', 'project management')",
                    },
                    "employee_name": {
                        "type": "string",
                        "description": "Optional: restrict to a specific employee",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_employee_profile",
            "description": (
                "Get the full profile for a specific employee, including all certifications and CV summary. "
                "Use when the user asks about a specific person by name."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "employee_name": {
                        "type": "string",
                        "description": "Full or partial name of the employee",
                    }
                },
                "required": ["employee_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_employees",
            "description": "Return a list of all employees in the system. Use when the user wants to know who is available.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_employee_cv_link",
            "description": (
                "Get the SharePoint link to an employee's CV document. "
                "Use when the user asks to see, open, or share a specific employee's CV."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "employee_name": {
                        "type": "string",
                        "description": "Full or partial name of the employee",
                    }
                },
                "required": ["employee_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_employee_cert_files",
            "description": (
                "Get download links for an employee's certificate or verification files (not their CV). "
                "Use when the user asks to share, see, or download an employee's certificates, "
                "certification letters, or verification documents. "
                "Returns a single URL for one file, or a zip download URL for multiple files."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "employee_name": {
                        "type": "string",
                        "description": "Full or partial name of the employee",
                    }
                },
                "required": ["employee_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_web",
            "description": (
                "Search the internet for information about certifications, technologies, or vendors. "
                "Useful to verify whether a technology relates to a domain, or to describe what a certification covers."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Web search query",
                    }
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "export_certifications_csv",
            "description": (
                "Export certification data to a downloadable CSV file. "
                "Handles all data fetching, expiry filtering, and CSV generation server-side in a SINGLE call "
                "— no pagination needed. Use this whenever the user asks to export, download, or view all "
                "certifications (or filtered subset) as a table or file. "
                "Prefer this over paginating search_certifications for any bulk export."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Optional: filter by technology, vendor, or certification name. Leave empty or use '*' to include all certifications.",
                    },
                    "expired_only": {
                        "type": "boolean",
                        "description": "If true, include only certifications that are expired (status='expired') or whose inferred expiry date is in the past. Default: false.",
                    },
                    "employee_name": {
                        "type": "string",
                        "description": "Optional: filter to a specific employee by name.",
                    },
                    "title": {
                        "type": "string",
                        "description": "Descriptive filename for the export (without .csv extension), e.g. 'expired_certifications'.",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_csv_report",
            "description": (
                "Export a non-certification list or table as a downloadable CSV file. "
                "Use for employee lists or other data (NOT certifications — use export_certifications_csv for those). "
                "Returns a download URL to share with the user."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "Descriptive filename for the export (without .csv extension), e.g. 'expired_certifications'",
                    },
                    "columns": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Ordered list of column header names",
                    },
                    "rows": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": "Array of objects where each key matches a column name",
                    },
                },
                "required": ["title", "columns", "rows"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "translate_cv_to_pdf",
            "description": (
                "Translate an employee's CV into a target language and generate a downloadable PDF "
                "in the Europass format. Use this whenever the user asks to translate a CV into "
                "another language. Returns a signed download URL for the generated PDF."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "employee_name": {
                        "type": "string",
                        "description": "Full or partial name of the employee whose CV should be translated",
                    },
                    "target_language": {
                        "type": "string",
                        "description": "ISO 639-1 language code to translate into (e.g. 'en', 'pt', 'es', 'fr', 'de')",
                    },
                },
                "required": ["employee_name", "target_language"],
            },
        },
    },
]

# ------------------------------------------------------------------ #
# System prompts                                                       #
# ------------------------------------------------------------------ #

_SYSTEM_PROMPT_EN = """\
You are Agent CV, an intelligent HR assistant for a technology company.
You have access to a database of employee certifications and CV documents.
Your job is to help HR managers and team leads find employees with specific skills, certifications, or experience.

TOOLS AVAILABLE:
• search_certifications — find employee certifications by technology, vendor, or keyword
• search_experience — search employee CVs for work experience and skills
• get_employee_profile — retrieve the full profile for a specific employee
• list_employees — list all employees in the system
• get_employee_cv_link — get the SharePoint link to an employee's CV document
• get_employee_cert_files — get download links for an employee's certificate/verification files (not the CV)
• search_web — look up external information about certifications or technologies
• export_certifications_csv — export certifications to a downloadable CSV file; handles all filtering server-side in ONE call (use for any certification export/bulk request)
• create_csv_report — export non-certification lists (e.g. employee lists) as a downloadable CSV file
• translate_cv_to_pdf — translate an employee's CV into a target language and generate a Europass-format PDF; returns a signed download URL

HOW TO RESPOND:
1. For any data query, use tools FIRST — never answer from memory or make assumptions
2. If a search returns no results, try alternate keywords (e.g. "AZ-900" for Azure, "RHCE" for Red Hat, "SC-200" for security)
3. For broad topics like "cybersecurity" or "cloud", search BOTH certifications AND experience
4. Respond in the SAME LANGUAGE as the user's message
5. Write in a conversational, helpful tone — like a knowledgeable colleague
6. FORMATTING — structure your replies clearly:
   - Group results by employee: use the employee name as a bold header (e.g. "**Name**")
   - Under each employee, list each certification as a top-level bullet: "- Certification Name"
   - Under each certification bullet, add indented sub-bullets (2 spaces + dash) for its details:
       "  - Vendor: Microsoft"
       "  - Issued: 2020-06-23"
       "  - Expires: 2025-06-23 (estimated)"
       "  - Status: expired"
   - Separate each employee block with a blank line
7. Be specific: list names and certification titles when found; avoid unnecessary hedging
8. End your reply with 1-2 relevant follow-up suggestions the user might find useful — keep them concise
9. If tools return no data, say clearly what you searched for and suggest alternatives
10. ALWAYS write exclusively in Latin script — never output characters from Georgian, Arabic, Cyrillic, Greek, or any other non-Latin alphabet, even as abbreviations or parenthetical notes. Accented Latin characters (e.g. é, ã, ç, ó, á, â, ê, ô, í, ú, à, õ) are standard Latin script and MUST be used correctly — do not strip or replace them with unaccented equivalents.
11. When the user asks to see or share an employee's **CV**, call get_employee_cv_link and include the returned URL as a plain hyperlink in your reply. Share only the CV document itself — do not include certification verification letters or other supplementary files. When the user asks to share or download an employee's **certificate files** (verification letters, certification PDFs), call get_employee_cert_files instead.
12. When answering questions about expired or expiring certifications, ALWAYS call search_certifications with status="any" — never pre-filter to "expired" or "expiring" in the tool call. After retrieving results, apply the following filtering rules:
    - EXPIRED: include only certs where status='expired' OR inferred_expiry_date is a past date (before today)
    - Do NOT include certs where inferred_expiry_date is null (these never expire)
    - Do NOT include certs where inferred_expiry_date is 'unknown' (expiry cannot be determined)
    - Do NOT include certs where inferred_expiry_date is a future date (these are "soon to expire", not expired — include them only if the user explicitly asks for expiring/soon-to-expire certs)
13. Certification results include an "inferred_expiry_date" field for records where the expiry date was not registered. If this field is a date (not null or "unknown"), use it as an estimated expiry and clearly label it as "estimated" or "inferred" in your answer. If it is null, the cert does not expire. If it is "unknown", you cannot infer an expiry date for that record.
14. PAGINATION — search_certifications returns 15 results per page. When has_more=true in the result, tell the user how many results remain and offer to show more. When the user asks to see more results (e.g. "show more", "next", "ver mais"), call search_certifications again with the same query/status and offset=next_offset from the previous result.
15. BULK EXPORTS — when the user asks to export, download, or compile all certifications into a table or file:
    - For certification data: call export_certifications_csv ONCE with the appropriate filters (e.g. expired_only=true for expired certs). Do NOT paginate search_certifications to collect data for an export.
    - For non-certification lists (e.g. all employees): use create_csv_report after collecting the data.
    - Reply with a brief message explaining the file was generated because the results are too large for chat, along with the download link.
16. CV TRANSLATION — when the user asks to translate an employee's CV into another language:
    - Call translate_cv_to_pdf with the employee's name and the target language ISO 639-1 code.
    - Infer the language code from the user's request (e.g. "French" → "fr", "Portuguese" → "pt", "Spanish" → "es").
    - Reply with a brief message and include the returned download URL as a hyperlink.
17. COMPETENCY ANALYSIS — when a user asks who has experience, skills, or certifications in a broad competency area (e.g. "storage", "networking", "security", "cloud", "virtualization"):
    a. Call search_certifications ONCE with the competency keyword — the tool automatically expands it server-side to all related vendors and technologies. No need to issue separate calls for each vendor.
    b. ALSO call search_experience with the same keyword to find employees who have CV evidence (work experience, projects) even without formal certifications.
    c. After gathering results, reason about implicit competencies: a Dell EMC certification demonstrates storage expertise even if "storage" does not appear in the cert title; a CCNA demonstrates networking expertise.
    d. In your answer, state the connection explicitly: "Maria has storage expertise, demonstrated by her Dell EMC certification and SAN administration experience in her CV." Do not just list raw results — interpret them.
    e. IMPORTANT: search_experience returns a "total_employees" count and an "employees" list. You MUST list ALL employees in that list — do not skip or summarise away any person. If someone has a lower relevance score they may still have real experience. Every employee returned is relevant.
    f. If both search_certifications and search_experience return results for the same employee, consolidate them into one block per person.
    g. BREVITY IN BROAD QUERIES — for a broad "who has X" question, list at most 2 certifications per employee and summarise the rest (e.g. "...and 4 more VMware certifications"). The goal is to list ALL people, not to exhaustively detail each one. The user can ask for full details on any specific person afterwards.
18. CERTIFICATE FILE SHARING — when the user asks to share, see, or download an employee's certificate files, verification letters, or certification documents (not their CV):
    - Call get_employee_cert_files with the employee's name.
    - If found=true and single=true: reply with a message and include the returned URL as a hyperlink.
    - If found=true and single=false: reply explaining there are multiple files and include the zip download URL as a hyperlink.
    - If found=false but sharepoint_urls is present: reply with the direct SharePoint URLs instead.
    - If found=false with no fallback: inform the user that no certificate files were found for that employee.
"""

_SYSTEM_PROMPT_PT = """\
És o Agent CV, um assistente de RH inteligente para uma empresa de tecnologia.
Tens acesso a uma base de dados de certificações e documentos de CV dos colaboradores.
O teu trabalho é ajudar gestores de RH e líderes de equipa a encontrar colaboradores com competências, certificações ou experiência específicas.

FERRAMENTAS DISPONÍVEIS:
• search_certifications — encontrar certificações por tecnologia, fornecedor ou palavra-chave
• search_experience — pesquisar CVs por experiência profissional e competências
• get_employee_profile — obter o perfil completo de um colaborador específico
• list_employees — listar todos os colaboradores no sistema
• get_employee_cv_link — obter o link do SharePoint para o CV de um colaborador
• get_employee_cert_files — obter links de transferência para os ficheiros de certificados/verificação de um colaborador (não o CV)
• search_web — pesquisar informação externa sobre certificações ou tecnologias
• export_certifications_csv — exportar certificações para um ficheiro CSV descarregável; trata toda a filtragem no servidor numa ÚNICA chamada (usar para qualquer exportação de certificações)
• create_csv_report — exportar listas não relacionadas com certificações (ex: lista de colaboradores) como ficheiro CSV descarregável
• translate_cv_to_pdf — traduzir o CV de um colaborador para outro idioma e gerar um PDF no formato Europass; devolve um URL de transferência assinado

COMO RESPONDER:
1. Para qualquer pergunta sobre dados, usa as ferramentas PRIMEIRO — nunca adivinhes
2. Se uma pesquisa não tiver resultados, tenta palavras-chave alternativas (ex: "AZ-900" para Azure, "RHCE" para Red Hat, "SC-200" para segurança)
3. Para tópicos amplos como "cibersegurança" ou "cloud", pesquisa TANTO em certificações COMO em experiência
4. Responde sempre no MESMO IDIOMA da mensagem do utilizador
5. Escreve num tom conversacional e útil — como um colega experiente
6. FORMATAÇÃO — estrutura as respostas de forma clara:
   - Agrupa os resultados por colaborador: usa o nome como cabeçalho em negrito (ex: "**Nome**")
   - Para cada colaborador, lista cada certificação como um marcador de primeiro nível: "- Nome da Certificação"
   - Sob cada marcador de certificação, adiciona sub-marcadores indentados (2 espaços + traço) com os detalhes:
       "  - Fornecedor: Microsoft"
       "  - Emissão: 2020-06-23"
       "  - Validade: 2025-06-23 (estimada)"
       "  - Estado: expired"
   - Separa cada bloco de colaborador com uma linha em branco
7. Sê específico: lista nomes e títulos de certificações quando encontrados; não sejas desnecessariamente cauteloso
8. Termina a resposta com 1-2 sugestões de perguntas de seguimento relevantes — mantém-nas concisas
9. Se as ferramentas não retornarem dados, diz claramente o que pesquisaste e sugere alternativas
10. Escreve SEMPRE exclusivamente em alfabeto latino — nunca uses caracteres do alfabeto georgiano, árabe, cirílico, grego ou qualquer outro alfabeto não-latino, mesmo em abreviaturas ou notas. Caracteres latinos acentuados (ex: é, ã, ç, ó, á, â, ê, ô, í, ú, à, õ) fazem parte do alfabeto latino e DEVEM ser usados corretamente — nunca os omitas ou substituas por versões sem acento. Em português, usa sempre a acentuação correta.
11. Quando o utilizador pedir para ver ou partilhar o **CV** de um colaborador, usa get_employee_cv_link e inclui o URL retornado como hiperligação na tua resposta. Partilha apenas o documento de CV — não incluas cartas de verificação de certificações nem outros documentos suplementares. Quando o utilizador pedir para partilhar ou transferir os **ficheiros de certificados** de um colaborador (cartas de verificação, PDFs de certificação), usa get_employee_cert_files em vez disso.
12. Quando responderes a perguntas sobre certificações vencidas ou a vencer, usa SEMPRE search_certifications com status="any" — nunca pré-filtres para "expired" ou "expiring" na chamada da ferramenta. Após obter os resultados, aplica as seguintes regras de filtragem:
    - VENCIDAS: inclui apenas certificações com status='expired' OU inferred_expiry_date com uma data no passado (anterior a hoje)
    - NÃO incluas certificações com inferred_expiry_date nulo (estas não expiram)
    - NÃO incluas certificações com inferred_expiry_date igual a 'unknown' (não é possível determinar a validade)
    - NÃO incluas certificações com inferred_expiry_date no futuro (estas estão "a vencer em breve", não vencidas — inclui-as apenas se o utilizador pedir explicitamente certificações a vencer)
13. Os resultados de certificações incluem um campo "inferred_expiry_date" para registos em que a data de validade não foi registada. Se este campo contiver uma data (não nulo nem "unknown"), usa-a como validade estimada e indica claramente que é uma estimativa na tua resposta. Se for nulo, a certificação não expira. Se for "unknown", não é possível inferir a data de validade desse registo.
14. PAGINAÇÃO — search_certifications devolve 15 resultados por página. Quando has_more=true no resultado, informa o utilizador de quantos resultados restam e oferece mostrar mais. Quando o utilizador pedir para ver mais resultados (ex: "mostrar mais", "ver mais", "próximos"), chama search_certifications novamente com o mesmo query/status e offset=next_offset do resultado anterior.
15. EXPORTAÇÕES — quando o utilizador pedir para exportar, descarregar ou compilar certificações numa tabela ou ficheiro:
    - Para dados de certificações: chama export_certifications_csv UMA VEZ com os filtros adequados (ex: expired_only=true para vencidas). NÃO uses paginação com search_certifications para recolher dados para uma exportação.
    - Para listas não relacionadas com certificações (ex: todos os colaboradores): usa create_csv_report depois de recolher os dados.
    - Responde com uma mensagem breve a explicar que o ficheiro foi gerado porque os resultados são demasiados para o chat, juntamente com o link de transferência.
16. TRADUÇÃO DE CV — quando o utilizador pedir para traduzir o CV de um colaborador para outro idioma:
    - Chama translate_cv_to_pdf com o nome do colaborador e o código ISO 639-1 do idioma de destino.
    - Infere o código do idioma a partir do pedido do utilizador (ex: "francês" → "fr", "inglês" → "en", "espanhol" → "es").
    - Responde com uma mensagem breve e inclui o URL de transferência devolvido como hiperligação.
17. ANÁLISE DE COMPETÊNCIAS — quando o utilizador perguntar quem tem experiência, competências ou certificações numa área ampla (ex: "storage", "redes", "segurança", "cloud", "virtualização"):
    a. Chama search_certifications UMA VEZ com a palavra-chave da competência — a ferramenta expande automaticamente no servidor para todos os fornecedores e tecnologias relacionados. Não é necessário fazer chamadas separadas por fornecedor.
    b. Chama TAMBÉM search_experience com a mesma palavra-chave para encontrar colaboradores com evidência no CV (experiência profissional, projetos) mesmo sem certificações formais.
    c. Após recolher os resultados, raciocina sobre competências implícitas: uma certificação Dell EMC demonstra competência em storage mesmo que "storage" não apareça no título; uma CCNA demonstra competência em redes.
    d. Na tua resposta, indica a ligação explicitamente: "A Maria tem competência em storage, demonstrada pela sua certificação Dell EMC e experiência em administração SAN no CV." Não te limites a listar resultados — interpreta-os.
    e. IMPORTANTE: search_experience devolve um campo "total_employees" e uma lista "employees". Deves listar TODOS os colaboradores dessa lista — não omitas nenhuma pessoa. Mesmo que alguém tenha uma pontuação de relevância mais baixa, pode ter experiência real. Todos os colaboradores devolvidos são relevantes.
    f. Se search_certifications e search_experience devolveram resultados para o mesmo colaborador, consolida-os num único bloco por pessoa.
    g. BREVIDADE EM PESQUISAS AMPLAS — para uma pergunta ampla do tipo "quem tem X", lista no máximo 2 certificações por colaborador e resume as restantes (ex: "...e mais 4 certificações VMware"). O objetivo é listar TODAS as pessoas, não detalhar exaustivamente cada uma. O utilizador pode pedir detalhes completos de qualquer pessoa depois.
18. PARTILHA DE FICHEIROS DE CERTIFICADOS — quando o utilizador pedir para partilhar, ver ou transferir os ficheiros de certificados, cartas de verificação ou documentos de certificação de um colaborador (não o CV):
    - Chama get_employee_cert_files com o nome do colaborador.
    - Se found=true e single=true: responde com uma mensagem e inclui o URL devolvido como hiperligação.
    - Se found=true e single=false: responde explicando que existem vários ficheiros e inclui o URL do zip como hiperligação.
    - Se found=false mas sharepoint_urls estiver presente: responde com os URLs diretos do SharePoint.
    - Se found=false sem alternativa: informa o utilizador que não foram encontrados ficheiros de certificados para esse colaborador.
"""

# ------------------------------------------------------------------ #
# Public types                                                         #
# ------------------------------------------------------------------ #


@dataclass
class ConversationState:
    language: str
    history: list[tuple[str, str]]


@dataclass(frozen=True)
class AgentQueryResult:
    language: str
    summary: str
    answer: str
    total_results: int
    shown_results: int
    has_more: bool
    analysis: QueryAnalysis
    rows_page: list[dict] = field(default_factory=list)
    show_certification_details: bool = False
    tool_calls_log: list[dict] = field(default_factory=list)


# ------------------------------------------------------------------ #
# In-memory conversation store                                         #
# ------------------------------------------------------------------ #

_STATE: dict[str, ConversationState] = {}
_STATE_LOCK = Lock()


# ------------------------------------------------------------------ #
# Entry point                                                          #
# ------------------------------------------------------------------ #


def handle_user_query(
    query_text: str,
    preferred_language: str | None,
    conversation_id: str | None,
) -> AgentQueryResult:
    state_key = (conversation_id or "default").strip() or "default"
    with _STATE_LOCK:
        prior = _STATE.get(state_key)

    language = preferred_language or _detect_language(query_text, prior)
    logger.info(
        "Agent: handling query (lang=%s, conv=%s) — %.80s", language, state_key, query_text
    )
    client = _get_chat_client()
    if client is None or not settings.azure_openai_chat_deployment:
        logger.warning(
            "Agent: no LLM client (endpoint=%s, deployment=%s) — using fallback",
            bool(settings.azure_openai_endpoint),
            settings.azure_openai_chat_deployment or "<unset>",
        )
        answer = _no_llm_fallback(language)
        _save_state(state_key, language, query_text, answer, prior)
        return AgentQueryResult(
            language=language,
            summary="",
            answer=answer,
            total_results=0,
            shown_results=0,
            has_more=False,
            analysis=_stub_analysis(language),
            tool_calls_log=[],
        )

    system_prompt = _SYSTEM_PROMPT_PT if language == "pt" else _SYSTEM_PROMPT_EN
    messages: list[dict] = [{"role": "system", "content": system_prompt}]

    if prior:
        for past_user, past_assistant in prior.history[-6:]:
            if past_user:
                messages.append({"role": "user", "content": past_user})
            if past_assistant:
                messages.append({"role": "assistant", "content": past_assistant})

    messages.append({"role": "user", "content": query_text})

    tool_call_count = 0
    tool_calls_log: list[dict] = []
    answer = ""
    answer_parts: list[str] = []
    # How many consecutive length-truncated continuations we allow before giving up
    _MAX_CONTINUATIONS = 4
    continuation_count = 0
    for iteration in range(MAX_AGENT_ITERATIONS):
        logger.debug("Agent: LLM call iteration %d (messages=%d)", iteration, len(messages))
        try:
            response = client.chat.completions.create(
                model=settings.azure_openai_chat_deployment,
                messages=messages,
                tools=TOOLS,
                tool_choice="auto",
                temperature=0.3,
                max_completion_tokens=2048,
            )
        except Exception as exc:
            # Detect Azure content-filter rejections and surface a clean error instead
            # of silently falling through to the generic "no answer" fallback.
            exc_str = str(exc)
            if "content_filter" in exc_str or "ResponsibleAIPolicyViolation" in exc_str:
                logger.error(
                    "Agent loop: content filter triggered on iteration %d — returning partial answer if available",
                    iteration,
                )
                if answer_parts:
                    # Return whatever was accumulated before the filter triggered
                    answer = "\n".join(answer_parts)
                    if language == "pt":
                        answer += (
                            "\n\n*(A resposta foi interrompida porque o contexto acumulado "
                            "excedeu os limites do serviço. Tente reformular o pedido com "
                            "um âmbito mais restrito.)*"
                        )
                    else:
                        answer += (
                            "\n\n*(Response was cut short because the accumulated context "
                            "exceeded the service limits. Try rephrasing with a narrower scope.)*"
                        )
                break
            logger.exception("Agent loop: LLM call failed on iteration %d", iteration)
            break

        msg = response.choices[0].message
        finish_reason = getattr(response.choices[0], "finish_reason", None)
        logger.debug(
            "Agent: iteration %d — finish_reason=%s, tool_calls=%s",
            iteration,
            finish_reason,
            len(msg.tool_calls) if msg.tool_calls else 0,
        )
        if not msg.tool_calls:
            chunk = (msg.content or "").strip()
            if finish_reason == "length" and continuation_count < _MAX_CONTINUATIONS:
                # LLM was cut off mid-response — save the partial content and ask it to continue
                continuation_count += 1
                if chunk:
                    answer_parts.append(chunk)
                    logger.debug(
                        "Agent: finish_reason=length at iteration %d (part %d, %d chars) — requesting continuation",
                        iteration,
                        continuation_count,
                        len(chunk),
                    )
                else:
                    logger.debug(
                        "Agent: finish_reason=length at iteration %d with empty chunk — requesting continuation",
                        iteration,
                    )
                messages.append({"role": "assistant", "content": msg.content or ""})
                continuation_prompt = (
                    "Continue a partir do ponto de corte."
                    if language == "pt"
                    else "Continue from where you left off."
                )
                messages.append({"role": "user", "content": continuation_prompt})
                continue
            if chunk:
                answer_parts.append(chunk)
            answer = "\n".join(answer_parts)
            logger.info(
                "Agent: final answer produced on iteration %d (%d chars, %d part(s))",
                iteration,
                len(answer),
                len(answer_parts),
            )
            break

        # Append assistant turn with tool calls
        messages.append({
            "role": "assistant",
            "content": msg.content or "",
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in msg.tool_calls
            ],
        })

        # Execute each requested tool
        for tool_call in msg.tool_calls:
            tool_call_count += 1
            try:
                args = json.loads(tool_call.function.arguments)
            except Exception:
                args = {}
            logger.debug("Agent: calling tool %s with args %s", tool_call.function.name, args)
            result = _dispatch_tool(tool_call.function.name, args)
            result_count = (
                result.get("total_found", len(result.get("results", [])))
                if isinstance(result, dict)
                else 0
            )
            logger.debug(
                "Agent: tool %s returned %d results", tool_call.function.name, result_count
            )
            tool_calls_log.append({
                "tool": tool_call.function.name,
                "args": args,
                "result_count": result_count,
            })
            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": json.dumps(result, ensure_ascii=False, default=str),
            })

    if not answer:
        logger.warning(
            "Agent: loop ended with no answer after %d tool call(s) — returning fallback",
            tool_call_count,
        )
        answer = (
            "Não foi possível processar a sua pergunta. Por favor tente novamente."
            if language == "pt"
            else "Unable to process your query. Please try again."
        )

    _save_state(state_key, language, query_text, answer, prior)

    return AgentQueryResult(
        language=language,
        summary="",
        answer=answer,
        total_results=tool_call_count,
        shown_results=0,
        has_more=False,
        analysis=_stub_analysis(language),
        tool_calls_log=tool_calls_log,
    )


# ------------------------------------------------------------------ #
# Tool dispatcher                                                      #
# ------------------------------------------------------------------ #


def _dispatch_tool(name: str, args: dict) -> Any:
    try:
        if name == "search_certifications":
            return _tool_search_certifications(
                **{k: v for k, v in args.items() if k in ("query", "employee_name", "status", "offset")}
            )
        if name == "search_experience":
            return _tool_search_experience(
                **{k: v for k, v in args.items() if k in ("query", "employee_name")}
            )
        if name == "get_employee_profile":
            return _tool_get_employee_profile(args.get("employee_name", ""))
        if name == "list_employees":
            return _tool_list_employees()
        if name == "export_certifications_csv":
            return _tool_export_certifications_csv(
                query=args.get("query", ""),
                expired_only=bool(args.get("expired_only", False)),
                employee_name=args.get("employee_name"),
                title=args.get("title"),
            )
        if name == "get_employee_cv_link":
            return _tool_get_employee_cv_link(args.get("employee_name", ""))
        if name == "get_employee_cert_files":
            return _tool_get_employee_cert_files(args.get("employee_name", ""))
        if name == "search_web":
            return _tool_search_web(args.get("query", ""))
        if name == "create_csv_report":
            return _tool_create_csv_report(
                title=args.get("title", "export"),
                columns=args.get("columns", []),
                rows=args.get("rows", []),
            )
        if name == "translate_cv_to_pdf":
            return _tool_translate_cv_to_pdf(
                employee_name=args.get("employee_name", ""),
                target_language=args.get("target_language", "en"),
            )
    except Exception:
        logger.exception("Agent: tool %s raised an exception with args %s", name, args)
        return {"error": f"Tool '{name}' failed unexpectedly"}
    logger.warning("Agent: unknown tool requested: %s", name)
    return {"error": f"Unknown tool: {name}"}


# ------------------------------------------------------------------ #
# Tool implementations                                                 #
# ------------------------------------------------------------------ #

# Certification validity periods by vendor/cert keyword (months).
# None = cert does not expire; omitted entries = cannot infer.
# Ordered from most-specific to least-specific so the first match wins.
_CERT_VALIDITY: list[tuple[str, int | None]] = [
    # Certs that do not expire
    ("itil foundation", None),
    ("itil 4 foundation", None),
    ("prince2 foundation", None),
    # Microsoft – 1 year (all role-based and specialty certs since 2020)
    # Fundamentals (az-900, ms-900, sc-900, dp-900) also expire after 1 yr
    ("microsoft", 12),
    ("azure", 12),
    # AWS – 3 years
    ("aws certified", 36),
    ("aws", 36),
    ("amazon web services", 36),
    # Red Hat – 3 years
    ("red hat", 36),
    ("rhce", 36),
    ("rhcsa", 36),
    ("rhcva", 36),
    # Cisco – 3 years
    ("cisco", 36),
    ("ccna", 36),
    ("ccnp", 36),
    ("ccie", 36),
    ("cct", 36),
    # CompTIA – 3 years (Continuing Education)
    ("comptia", 36),
    ("security+", 36),
    ("network+", 36),
    ("cysa+", 36),
    ("pentest+", 36),
    ("casp+", 36),
    # Google Cloud – 2 years
    ("google cloud", 24),
    ("gcp", 24),
    # Kubernetes / CNCF – 2 years
    ("cka", 24),
    ("ckad", 24),
    ("cks", 24),
    # VMware – 2 years
    ("vmware", 24),
    ("vcp", 24),
    # Fortinet – 2 years
    ("fortinet", 24),
    ("nse ", 24),  # trailing space avoids matching "nsec" etc.
    # Palo Alto Networks – 2 years
    ("palo alto", 24),
    ("pcnsa", 24),
    ("pcnse", 24),
    # HashiCorp – 2 years
    ("hashicorp", 24),
    ("terraform associate", 24),
    # PMI – 3 years
    ("pmp", 36),
    ("pmi", 36),
    # ISACA – 3 years
    ("cism", 36),
    ("cisa", 36),
    ("crisc", 36),
    # ISC2 – 3 years
    ("cissp", 36),
    ("ccsp", 36),
    ("sscp", 36),
]


def _infer_expiry_date(cert_name: str, vendor: str, issue_date: object) -> object:
    """Return an inferred expiry date (datetime.date) or None (cert does not expire).

    Returns the sentinel string ``"unknown"`` when no rule matches and inference
    is not possible — callers should propagate this distinction to the LLM.
    """
    import datetime

    if issue_date is None:
        return "unknown"

    haystack = f"{cert_name} {vendor}".lower()

    for keyword, months in _CERT_VALIDITY:
        if keyword in haystack:
            if months is None:
                return None  # does not expire
            # dateutil not guaranteed; use manual month arithmetic
            if isinstance(issue_date, (datetime.date, datetime.datetime)):
                base = issue_date if isinstance(issue_date, datetime.date) else issue_date.date()
            else:
                try:
                    base = datetime.date.fromisoformat(str(issue_date)[:10])
                except ValueError:
                    return "unknown"
            # Add months manually to avoid dateutil dependency
            month = base.month + months
            year = base.year + (month - 1) // 12
            month = (month - 1) % 12 + 1
            try:
                return datetime.date(year, month, base.day)
            except ValueError:
                # Handle e.g. Feb 29 → Feb 28
                return datetime.date(year, month, 28)

    return "unknown"  # no rule matched


_PAGE_SIZE = 15

# Maps a broad competency keyword (normalize_text form: no accents, lowercase)
# to the set of vendor/technology tokens that should be added to the SQL search.
# This runs server-side so even a single-keyword LLM call returns full results.
_COMPETENCY_EXPANSIONS: dict[str, list[str]] = {
    "storage":        ["dell", "emc", "netapp", "veeam", "san", "nas", "vsan", "backup", "pure"],
    "cloud":          ["azure", "aws", "amazon", "gcp", "google"],
    "networking":     ["cisco", "ccna", "ccnp", "juniper", "fortinet", "routing", "switching"],
    "network":        ["cisco", "ccna", "ccnp", "juniper", "fortinet"],
    "redes":          ["cisco", "ccna", "ccnp", "juniper", "fortinet", "routing", "switching"],
    "security":       ["cissp", "comptia", "palo", "fortinet", "soc", "siem", "pentest", "ceh", "cism", "cisa"],
    "seguranca":      ["cissp", "comptia", "palo", "fortinet", "soc", "siem", "pentest", "ceh", "cism", "cisa"],
    "virtualization": ["vmware", "vsphere", "vcp", "nutanix", "kvm", "hyper"],
    "virtualizacao":  ["vmware", "vsphere", "vcp", "nutanix", "kvm", "hyper"],
    "backup":         ["veeam", "dell", "emc", "netbackup", "commvault", "arcserve"],
    "database":       ["oracle", "mssql", "mysql", "postgresql", "mongodb", "dba"],
    "base de dados":  ["oracle", "mssql", "mysql", "postgresql", "mongodb", "dba"],
}


def _tool_search_certifications(
    query: str,
    employee_name: str | None = None,
    status: str = "any",
    offset: int = 0,
) -> dict:
    from agent_cv.services.retrieval_service import _embed_query, _search_semantic_chunks

    structured = _sql_search_certifications(query, employee_name, status)
    total = len(structured)
    page = structured[offset: offset + _PAGE_SIZE]

    query_vector = _embed_query(query)
    semantic_excerpts: list[dict] = []
    if query_vector:
        scoped = [employee_name] if employee_name else []
        for s in _search_semantic_chunks(query_vector, "certifications", scoped):
            semantic_excerpts.append({
                "employee_name": s.employee_name,
                "source": s.source,
                "relevance_score": round(s.score, 3),
                "excerpt": s.text[:300],
            })

    result: dict = {
        "certifications": page,
        "semantic_excerpts": semantic_excerpts[:5],
        "total_found": total,
        "offset": offset,
        "page_size": _PAGE_SIZE,
    }
    if offset + _PAGE_SIZE < total:
        result["has_more"] = True
        result["next_offset"] = offset + _PAGE_SIZE
        result["note"] = (
            f"Showing results {offset + 1}–{offset + len(page)} of {total}. "
            f"Call again with offset={offset + _PAGE_SIZE} to get the next page."
        )
    else:
        result["has_more"] = False
        result["note"] = f"Showing results {offset + 1}–{offset + len(page)} of {total}. All results shown."
    return result


def _sql_search_certifications(
    query: str,
    employee_name: str | None,
    status: str,
) -> list[dict]:
    # Words that describe intent but don't identify a specific cert or vendor.
    # When the query is made up entirely of these, skip keyword filtering so
    # the tool returns ALL certs matching the status/employee filters — not a
    # random subset that happens to contain those words in a cert name.
    _GENERIC_TOKENS = {
        "all", "any", "expired", "expiring", "active", "certification",
        "certifications", "certificacao", "certificacoes", "certificate",
        "certificates", "list", "show", "give", "get", "find", "search",
        "employee", "employees", "colaborador", "colaboradores", "todas",
        "todos", "vencidas", "vencidos", "validade", "status", "with", "has",
        "that", "are", "the", "and", "for", "por", "com", "que", "dos", "das",
    }

    norm = normalize_text(query)
    raw_tokens = [t for t in norm.split() if len(t) >= 3][:6]
    # Only use tokens that are likely cert/vendor/tech names, not generic words
    tokens = [t for t in raw_tokens if t not in _GENERIC_TOKENS]

    # Expand any broad competency keywords into related vendor/tech tokens.
    # This ensures e.g. 'storage' also matches Dell EMC, Veeam, NetApp, etc.
    _expanded: list[str] = list(tokens)
    for _t in tokens:
        _expanded.extend(_COMPETENCY_EXPANSIONS.get(_t, []))
    tokens = list(dict.fromkeys(_expanded))  # deduplicate, preserve order

    where_parts: list[str] = []
    params: list[Any] = []

    if employee_name:
        where_parts.append("lower(e.full_name) like %s")
        params.append(f"%{normalize_text(employee_name)}%")

    if status == "active":
        where_parts.append("c.status <> 'expired'")
    elif status == "expired":
        where_parts.append("c.status = 'expired'")

    if tokens:
        token_clauses: list[str] = []
        for token in tokens:
            token_clauses.append(
                "(lower(c.cert_name) like %s "
                "or lower(coalesce(v.vendor_name, '')) like %s "
                "or lower(e.full_name) like %s)"
            )
            params.extend([f"%{token}%", f"%{token}%", f"%{token}%"])
        where_parts.append("(" + " or ".join(token_clauses) + ")")

    sql = """
        select
            e.full_name as employee_name,
            c.cert_name as certification_name,
            coalesce(v.vendor_name, 'Unknown') as vendor,
            c.status,
            c.issue_date,
            c.expiry_date
        from certifications c
        join employees e on e.employee_id = c.employee_id
        left join vendors v on v.vendor_id = c.vendor_id
    """
    if where_parts:
        sql += " where " + " and ".join(where_parts)
    sql += " order by e.full_name, c.expiry_date nulls last limit 200"

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = [dict(row) for row in cur.fetchall()]
    except Exception:
        logger.exception("Agent: _sql_search_certifications failed")
        return []

    # Enrich rows that have no expiry_date with an inferred estimate
    for row in rows:
        if row.get("expiry_date") is None and row.get("issue_date") is not None:
            inferred = _infer_expiry_date(
                row.get("certification_name", ""),
                row.get("vendor", ""),
                row["issue_date"],
            )
            row["inferred_expiry_date"] = inferred  # None means "does not expire"
            row["expiry_date_is_inferred"] = inferred is not None
        else:
            row["inferred_expiry_date"] = None
            row["expiry_date_is_inferred"] = False

    return rows


def _tool_search_experience(
    query: str,
    employee_name: str | None = None,
) -> dict:
    from agent_cv.services.retrieval_service import _embed_query, _search_semantic_chunks

    query_vector = _embed_query(query)
    if not query_vector:
        return {"experience_snippets": [], "total_found": 0, "note": "Embedding unavailable"}

    scoped = [employee_name] if employee_name else []
    snippets = _search_semantic_chunks(query_vector, "experience", scoped)

    # Group by employee so the LLM clearly sees how many distinct people were found
    # and is not tempted to skip lower-ranked employees.
    employees: dict[str, dict] = {}
    for s in snippets:
        name = s.employee_name
        if name not in employees:
            employees[name] = {
                "employee_name": name,
                "best_relevance_score": round(s.score, 3),
                "excerpts": [],
            }
        if len(employees[name]["excerpts"]) < 2:
            employees[name]["excerpts"].append(s.text[:400])

    employee_list = sorted(employees.values(), key=lambda x: x["best_relevance_score"], reverse=True)

    return {
        "employees": employee_list,
        "total_employees": len(employee_list),
        "total_found": len(employee_list),
    }


def _tool_get_employee_profile(employee_name: str) -> dict:
    norm = normalize_text(employee_name)
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "select employee_id, full_name, primary_language, department "
                    "from employees where lower(full_name) like %s limit 3",
                    (f"%{norm}%",),
                )
                employees = cur.fetchall()
                if not employees:
                    return {"error": f"No employee found matching '{employee_name}'"}

                emp = employees[0]
                emp_id = emp["employee_id"]

                cur.execute(
                    """
                    select c.cert_name, coalesce(v.vendor_name, 'Unknown') as vendor,
                           c.status, c.issue_date, c.expiry_date
                    from certifications c
                    left join vendors v on v.vendor_id = c.vendor_id
                    where c.employee_id = %s
                    order by c.expiry_date nulls last
                    """,
                    (emp_id,),
                )
                certs = [dict(r) for r in cur.fetchall()]

                cur.execute(
                    """
                    select cs.section_type, left(cs.section_text, 500) as section_text
                    from cv_sections cs
                    join document_versions dv on dv.document_version_id = cs.document_version_id
                    join source_documents sd on sd.document_id = dv.document_id
                    where sd.employee_id = %s and dv.is_current = true
                    order by cs.section_type
                    limit 10
                    """,
                    (emp_id,),
                )
                sections = [dict(r) for r in cur.fetchall()]

        return {
            "employee": {
                "name": emp["full_name"],
                "department": emp.get("department"),
                "language": emp.get("primary_language"),
            },
            "certifications": certs,
            "cv_sections": sections,
            "other_matches": [e["full_name"] for e in employees[1:]],
        }
    except Exception:
        logger.exception("Agent: get_employee_profile failed for '%s'", employee_name)
        return {"error": "Failed to retrieve employee profile"}


def _tool_list_employees() -> dict:
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "select full_name, department, primary_language from employees order by full_name"
                )
                rows = cur.fetchall()
        return {"employees": [dict(r) for r in rows], "total": len(rows)}
    except Exception:
        logger.exception("Agent: list_employees failed")
        return {"employees": [], "total": 0}


def _tool_get_employee_cv_link(employee_name: str) -> dict:
    """Return download URLs for a specific employee's CV documents."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT e.full_name, sd.document_id, sd.original_filename,
                       sd.sharepoint_web_url, sd.sharepoint_item_id, sd.source_path
                FROM source_documents sd
                JOIN employees e ON sd.employee_id = e.employee_id
                WHERE e.full_name ILIKE %s
                  AND EXISTS (
                    SELECT 1 FROM document_versions dv
                    JOIN cv_sections cs ON cs.document_version_id = dv.document_version_id
                    WHERE dv.document_id = sd.document_id AND dv.is_current = true
                  )
                ORDER BY sd.created_at DESC
                """,
                (f"%{employee_name}%",),
            )
            rows = cur.fetchall()

    if not rows:
        return {
            "found": False,
            "message": f"No CV found for '{employee_name}'. The document may not have been ingested yet.",
        }

    from agent_cv.api.routes import generate_cv_download_url

    documents = []
    for r in rows:
        url = r["sharepoint_web_url"] or generate_cv_download_url(r["document_id"])
        documents.append(
            {
                "employee": r["full_name"],
                "filename": r["original_filename"],
                "url": url,
            }
        )

    return {"found": True, "documents": documents}


def _tool_get_employee_cert_files(employee_name: str) -> dict:
    """Return download links for an employee's non-CV source documents (cert letters, etc.)."""
    import os
    import uuid
    import zipfile

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT e.full_name, sd.document_id, sd.original_filename,
                       sd.sharepoint_web_url, sd.source_path
                FROM source_documents sd
                JOIN employees e ON sd.employee_id = e.employee_id
                WHERE e.full_name ILIKE %s
                  AND NOT EXISTS (
                    SELECT 1 FROM document_versions dv
                    JOIN cv_sections cs ON cs.document_version_id = dv.document_version_id
                    WHERE dv.document_id = sd.document_id AND dv.is_current = true
                  )
                ORDER BY sd.created_at DESC
                """,
                (f"%{employee_name}%",),
            )
            rows = cur.fetchall()

    if not rows:
        return {
            "found": False,
            "message": f"No certificate files found for '{employee_name}'.",
        }

    from agent_cv.api.routes import generate_cv_download_url, generate_export_url

    if len(rows) == 1:
        r = rows[0]
        url = r["sharepoint_web_url"] or generate_cv_download_url(r["document_id"])
        return {
            "found": True,
            "single": True,
            "employee": r["full_name"],
            "filename": r["original_filename"],
            "url": url,
        }

    # Multiple files — build a zip
    export_id = str(uuid.uuid4())
    exports_dir = "/tmp/agent_cv_exports"
    os.makedirs(exports_dir, exist_ok=True)
    zip_path = f"{exports_dir}/{export_id}.zip"
    filenames = []
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for r in rows:
            src = r["source_path"]
            if src and os.path.isfile(src):
                zf.write(src, arcname=r["original_filename"])
                filenames.append(r["original_filename"])
            else:
                logger.warning(
                    "_tool_get_employee_cert_files: file not on disk for document_id=%s",
                    r["document_id"],
                )

    if not filenames:
        os.remove(zip_path)
        return {
            "found": False,
            "message": f"Certificate files for '{employee_name}' are stored in SharePoint and cannot be packaged locally.",
            "sharepoint_urls": [
                {"filename": r["original_filename"], "url": r["sharepoint_web_url"]}
                for r in rows
                if r["sharepoint_web_url"]
            ],
        }

    return {
        "found": True,
        "single": False,
        "employee": rows[0]["full_name"],
        "file_count": len(filenames),
        "filenames": filenames,
        "url": generate_export_url(export_id),
    }


def _sql_export_certifications(
    query: str,
    employee_name: str | None,
) -> list[dict]:
    """Fetch ALL matching certifications with no page-size cap (up to 10 000 rows).

    Used by export_certifications_csv so the server-side export is never
    limited by the 200-row cap that _sql_search_certifications applies for
    interactive pagination.
    """
    _GENERIC_TOKENS = {
        "all", "any", "expired", "expiring", "active", "certification",
        "certifications", "certificacao", "certificacoes", "certificate",
        "certificates", "list", "show", "give", "get", "find", "search",
        "employee", "employees", "colaborador", "colaboradores", "todas",
        "todos", "vencidas", "vencidos", "validade", "status", "with", "has",
        "that", "are", "the", "and", "for", "por", "com", "que", "dos", "das",
    }

    norm = normalize_text(query or "")
    raw_tokens = [t for t in norm.split() if len(t) >= 3][:6]
    tokens = [t for t in raw_tokens if t not in _GENERIC_TOKENS]

    where_parts: list[str] = []
    params: list[Any] = []

    if employee_name:
        where_parts.append("lower(e.full_name) like %s")
        params.append(f"%{normalize_text(employee_name)}%")

    if tokens:
        token_clauses: list[str] = []
        for token in tokens:
            token_clauses.append(
                "(lower(c.cert_name) like %s "
                "or lower(coalesce(v.vendor_name, '')) like %s "
                "or lower(e.full_name) like %s)"
            )
            params.extend([f"%{token}%", f"%{token}%", f"%{token}%"])
        where_parts.append("(" + " or ".join(token_clauses) + ")")

    sql = """
        select
            e.full_name as employee_name,
            c.cert_name as certification_name,
            coalesce(v.vendor_name, 'Unknown') as vendor,
            c.status,
            c.issue_date,
            c.expiry_date
        from certifications c
        join employees e on e.employee_id = c.employee_id
        left join vendors v on v.vendor_id = c.vendor_id
    """
    if where_parts:
        sql += " where " + " and ".join(where_parts)
    sql += " order by e.full_name, c.expiry_date nulls last limit 10000"

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = [dict(row) for row in cur.fetchall()]
    except Exception:
        logger.exception("Agent: _sql_export_certifications failed")
        return []

    # Enrich with inferred expiry dates (same logic as _sql_search_certifications)
    for row in rows:
        if row.get("expiry_date") is None and row.get("issue_date") is not None:
            inferred = _infer_expiry_date(
                row.get("certification_name", ""),
                row.get("vendor", ""),
                row["issue_date"],
            )
            row["inferred_expiry_date"] = inferred
            row["expiry_date_is_inferred"] = inferred is not None
        else:
            row["inferred_expiry_date"] = None
            row["expiry_date_is_inferred"] = False

    return rows


def _tool_export_certifications_csv(
    query: str = "",
    expired_only: bool = False,
    employee_name: str | None = None,
    title: str | None = None,
) -> dict:
    """Fetch all matching certifications, apply expiry filtering, and export to CSV.

    Does everything server-side in a single call — no LLM-side pagination needed.
    """
    import csv
    import datetime
    import os
    import re
    import uuid

    from agent_cv.api.routes import generate_export_url

    rows = _sql_export_certifications(query or "", employee_name)
    today = datetime.date.today()

    if expired_only:
        filtered: list[dict] = []
        for row in rows:
            # Already explicitly marked expired by the extractor
            if row.get("status") == "expired":
                filtered.append(row)
                continue
            # Recorded expiry date is in the past
            expiry = row.get("expiry_date")
            if expiry is not None:
                d = expiry if isinstance(expiry, datetime.date) else None
                if d and d < today:
                    filtered.append(row)
                continue
            # No recorded expiry — check inferred date
            inferred = row.get("inferred_expiry_date")
            if isinstance(inferred, datetime.date) and inferred < today:
                filtered.append(row)
        rows = filtered

    if not rows:
        return {
            "found": False,
            "message": "No certifications found matching the given filters.",
        }

    safe_title = re.sub(r"[^\w\-]", "_", (title or "certifications").strip()) or "certifications"
    export_id = str(uuid.uuid4())
    export_dir = "/tmp/agent_cv_exports"
    os.makedirs(export_dir, exist_ok=True)
    csv_path = os.path.join(export_dir, f"{export_id}.csv")

    columns = [
        "employee_name", "certification_name", "vendor",
        "status", "issue_date", "expiry_date", "inferred_expiry_date",
    ]

    def _fmt_date(v: object) -> str:
        if v is None:
            return ""
        if v == "unknown":
            return "unknown"
        return str(v)

    try:
        with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow({
                    "employee_name": row.get("employee_name", ""),
                    "certification_name": row.get("certification_name", ""),
                    "vendor": row.get("vendor", ""),
                    "status": row.get("status", ""),
                    "issue_date": _fmt_date(row.get("issue_date")),
                    "expiry_date": _fmt_date(row.get("expiry_date")),
                    "inferred_expiry_date": (
                        "N/A" if row.get("inferred_expiry_date") is None
                        else _fmt_date(row.get("inferred_expiry_date"))
                    ),
                })
    except Exception:
        logger.exception("Agent: export_certifications_csv failed to write %s", csv_path)
        return {"error": "Failed to generate CSV file"}

    url = generate_export_url(export_id)
    logger.info(
        "Agent: created certifications CSV export %s — %d rows (expired_only=%s)",
        export_id, len(rows), expired_only,
    )
    return {
        "url": url,
        "filename": f"{safe_title}.csv",
        "row_count": len(rows),
    }


def _tool_search_web(query: str) -> dict:
    try:
        from duckduckgo_search import DDGS  # type: ignore

        with DDGS(timeout=8) as ddgs:
            hits = [
                f"{r['title']}: {r['body']}"
                for r in ddgs.text(query, max_results=5)
                if r.get("title") and r.get("body")
            ]
        return {"results": hits[:5], "total_found": len(hits)}
    except Exception:
        logger.exception("Agent: _tool_search_web failed")
        return {"results": [], "error": "Web search unavailable"}


def _tool_create_csv_report(
    title: str,
    columns: list[str],
    rows: list[dict],
) -> dict:
    """Write rows to a temporary CSV file and return a signed download URL."""
    import csv
    import os
    import re
    import uuid

    from agent_cv.api.routes import generate_export_url

    # Sanitise title to a safe filename (keep alphanumeric, dashes, underscores)
    safe_title = re.sub(r"[^\w\-]", "_", title.strip()) or "export"
    export_id = str(uuid.uuid4())
    export_dir = "/tmp/agent_cv_exports"
    os.makedirs(export_dir, exist_ok=True)
    csv_path = os.path.join(export_dir, f"{export_id}.csv")

    try:
        with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
    except Exception:
        logger.exception("Agent: create_csv_report failed to write %s", csv_path)
        return {"error": "Failed to generate CSV file"}

    url = generate_export_url(export_id)
    logger.info("Agent: created CSV export %s with %d rows", export_id, len(rows))
    return {
        "url": url,
        "filename": f"{safe_title}.csv",
        "row_count": len(rows),
    }


def _tool_translate_cv_to_pdf(employee_name: str, target_language: str) -> dict:
    """Translate an employee's CV and return a signed PDF download URL."""
    from agent_cv.services.translation_service import translate_and_export_cv
    from agent_cv.api.routes import generate_export_url

    result = translate_and_export_cv(employee_name, target_language)
    if result.get("error"):
        return {"error": result["error"]}

    export_id = result["export_id"]
    url = generate_export_url(export_id)
    logger.info(
        "Agent: translated CV for %s to %s — export_id=%s",
        result["employee_name"],
        target_language,
        export_id,
    )
    return {
        "url": url,
        "employee_name": result["employee_name"],
        "target_language": result["target_language"],
        "filename": f"{result['employee_name'].replace(' ', '_')}_CV_{target_language.upper()}.pdf",
    }


# ------------------------------------------------------------------ #
# Helpers                                                              #
# ------------------------------------------------------------------ #


def _detect_language(query: str, prior: ConversationState | None) -> str:
    norm = normalize_text(query)
    pt_score = sum(
        1 for m in {"quem", "tem", "qual", "quais", "certificacoes", "experiencia",
                    "colaborador", "ola", "obrigado", "inclua", "incluir", "pessoas",
                    "vencidas", "vencer", "perto", "mostra", "mostrar", "lista", "quero"}
        if m in norm
    )
    en_score = sum(
        1 for m in {"who", "has", "which", "what", "certifications", "experience",
                    "employee", "hello", "thanks", "include", "show", "list", "expired",
                    "expiring", "soon", "find", "search"}
        if m in norm
    )
    if pt_score > en_score:
        return "pt"
    if en_score > pt_score:
        return "en"
    # Ambiguous — keep prior language if available, else default to "en"
    return prior.language if prior else "en"


def _no_llm_fallback(language: str) -> str:
    if language == "pt":
        return "O serviço de linguagem não está configurado. Verifique as variáveis AZURE_OPENAI_* no ficheiro .env."
    return "Language service is not configured. Please check AZURE_OPENAI_* environment variables."


def _stub_analysis(language: str) -> QueryAnalysis:
    return QueryAnalysis(
        query_type="chat",
        language=language,
        normalized_query="",
        tokens=[],
        vendor_terms=[],
        employee_terms=[],
        expired_only=False,
        active_only=False,
        storage_only=False,
        wants_certification_details=False,
        wants_employee_names_only=False,
        wants_experience_summary=False,
    )


def _save_state(
    state_key: str,
    language: str,
    user_msg: str,
    assistant_msg: str,
    prior: ConversationState | None,
) -> None:
    history = list(prior.history if prior else [])
    history.append((user_msg.strip(), assistant_msg.strip()))
    with _STATE_LOCK:
        _STATE[state_key] = ConversationState(
            language=language,
            history=history[-8:],
        )


@lru_cache(maxsize=1)
def _get_chat_client() -> AzureOpenAI | None:
    if not settings.azure_openai_endpoint or not settings.azure_openai_api_key:
        return None
    try:
        return AzureOpenAI(
            azure_endpoint=settings.azure_openai_endpoint,
            api_key=settings.azure_openai_api_key,
            api_version=settings.azure_openai_api_version,
        )
    except Exception:
        return None
