"""
Chat Web Search Integration
Automatically enhances chat messages with web search when needed

COMPREHENSIVE DECISION MATRIX:
==============================

This module implements a sophisticated scoring-based decision matrix to determine
when web search is necessary for accurate, current information. The system uses
multiple layers of pattern matching and scoring to minimize false positives while
ensuring all time-sensitive queries trigger web search.

SCORING SYSTEM:
--------------
+3 points: Real-time patterns (highest confidence - e.g., "current price", "latest news")
+2 points: Strong indicators (time-sensitive words, financial data, news, weather)
+1 point:  Domain-specific patterns (crypto, stocks, sports, tech releases)
-2 points: General knowledge indicators (definitions, explanations, how-to)
-4 points: Maximum penalty for general knowledge (capped)
-5 points: Exclusion patterns (immediate rejection unless time-sensitive words present)

THRESHOLDS:
-----------
- Standard threshold: 2 points
- Real-time pattern matched: 1 point (very high confidence)
- Short messages (<20 chars): 3 points (reduces false positives)
- Exclusion matched but time-sensitive present: 3 points (requires higher confidence)

PATTERN CATEGORIES:
------------------
1. STRONG_WEB_SEARCH_INDICATORS: 50+ keywords/phrases indicating current info needed
2. REAL_TIME_PATTERNS: 20+ regex patterns for high-confidence time-sensitive queries
3. EXCLUSION_PATTERNS: 30+ regex patterns that immediately reject general knowledge
4. GENERAL_KNOWLEDGE_INDICATORS: 40+ keywords indicating static knowledge
5. DOMAIN_PATTERNS: 8+ domain-specific patterns (financial, weather, news, sports, etc.)

EDGE CASE HANDLING:
-------------------
- Time-sensitive words override exclusion patterns
- Short messages require higher scores
- General knowledge penalties are capped
- Multiple pattern matches are additive
- Real-time patterns lower threshold (high confidence)

This comprehensive system ensures web search is triggered only when truly needed,
avoiding unnecessary API calls while catching all legitimate time-sensitive queries.
"""
import logging
import re
from typing import List, Dict, Any, Optional
from app.core.ollama_web_search import OllamaWebSearchService

logger = logging.getLogger(__name__)

# Keywords that STRONGLY suggest a query needs CURRENT/REAL-TIME web search
STRONG_WEB_SEARCH_INDICATORS = [
    # Time-sensitive indicators (comprehensive)
    "current", "currently", "latest", "today", "now", "right now", "as of", "as of now",
    "recent", "recently", "newest", "new", "just", "happened", "happening", "happens",
    "this year", "this month", "this week", "this day", "yesterday", "tomorrow",
    "live", "real-time", "real time", "up-to-date", "up to date", "current status",
    "ongoing", "in progress", "active", "latest update", "most recent",
    "at the moment", "at this time", "present", "presently", "nowadays",
    
    # Financial/market data (always needs current info - comprehensive)
    "price", "prices", "pricing", "cost", "costs", "costing", "value", "valuation",
    "stock price", "share price", "crypto price", "cryptocurrency price",
    "bitcoin", "btc", "ethereum", "eth", "cryptocurrency", "crypto", "altcoin", "altcoins",
    "market", "markets", "trading", "exchange rate", "exchange rates", "forex", "fx",
    "dollar", "dollars", "usd", "euro", "euros", "eur", "pound", "pounds", "gbp",
    "yen", "yuan", "currency", "currencies", "crypto market", "stock market",
    "nasdaq", "dow jones", "dow", "s&p", "s&p 500", "sp500", "ftse", "nikkei",
    "dax", "cac", "hang seng", "commodity", "commodities", "futures", "options",
    "market cap", "market capitalization", "volume", "trading volume",
    
    # News and events (comprehensive)
    "news", "breaking", "breaking news", "update", "updates", "announcement", "announcements",
    "event", "events", "happened", "occurred", "released", "published", "launched",
    "headline", "headlines", "report", "reports", "story", "stories", "article", "articles",
    "press release", "press conference", "statement", "statements",
    
    # Weather (always needs current - comprehensive)
    "weather", "forecast", "forecasts", "temperature", "temp", "temperatures",
    "rain", "raining", "rainy", "snow", "snowing", "snowy", "wind", "windy",
    "humidity", "precipitation", "cloudy", "sunny", "storm", "storms", "hurricane",
    "tornado", "climate", "conditions", "weather conditions", "weather report",
    
    # Sports scores/results (comprehensive)
    "score", "scores", "scoring", "game", "games", "match", "matches", "result", "results",
    "won", "win", "wins", "winning", "lost", "lose", "loses", "losing",
    "championship", "championships", "tournament", "tournaments", "league", "leagues",
    "team", "teams", "player", "players", "athlete", "athletes", "sports",
    
    # Current events/politics (comprehensive)
    "election", "elections", "vote", "votes", "voting", "poll", "polls", "polling",
    "president", "presidential", "senate", "congress", "parliament", "government",
    "policy", "policies", "legislation", "bill", "bills", "law", "laws",
    "court", "courts", "judge", "judges", "ruling", "rulings", "verdict",
    
    # Technology/software releases (needs current)
    "release", "releases", "version", "versions", "update", "updates", "patch", "patches",
    "launch", "launches", "launched", "beta", "alpha", "preview", "announcement",
    
    # Business/company info (needs current)
    "earnings", "revenue", "profit", "quarterly", "annual report", "ipo", "merger",
    "acquisition", "stock split", "dividend", "dividends",
    
    # Travel/transportation (needs current)
    "flight", "flights", "airline", "airlines", "delay", "delays", "cancelled",
    "traffic", "road conditions", "transit", "public transport",
    
    # Health/medical (needs current for outbreaks, etc.)
    "outbreak", "pandemic", "epidemic", "cases", "deaths", "vaccine", "vaccines",
    "health alert", "medical emergency",
]

# Keywords that suggest the question is about GENERAL KNOWLEDGE (doesn't need web search)
GENERAL_KNOWLEDGE_INDICATORS = [
    # Definition/explanation patterns
    "what is", "what are", "what was", "what were", "what does", "what do",
    "who is", "who are", "who was", "who were", "who did", "who does",
    "explain", "explains", "explained", "explaining", "explanation",
    "define", "defines", "defined", "defining", "definition", "definitions",
    "meaning", "meanings", "means", "meant", "mean",
    "describe", "describes", "described", "describing", "description",
    
    # How-to and process questions
    "how does", "how do", "how did", "how to", "how can", "how could",
    "how would", "how should", "how might", "how may",
    "steps to", "step by step", "process", "procedure", "method", "way to",
    
    # Why questions (usually conceptual)
    "why", "why is", "why are", "why was", "why were", "why did", "why does",
    "why do", "why would", "why should", "why might", "why may",
    "reason", "reasons", "reasoning", "because", "causes", "caused",
    
    # Comparison questions
    "difference between", "differences between", "difference among",
    "compare", "compares", "compared", "comparing", "comparison",
    "versus", "vs", "vs.", "v.", "against", "versus",
    "similar", "similarity", "similarities", "same", "different",
    
    # Example/listing questions
    "example", "examples", "instance", "instances", "case", "cases",
    "list", "lists", "listing", "enumerate", "itemize",
    "types of", "kinds of", "sorts of", "varieties of", "categories of",
    "name", "names", "naming", "identify", "identifies",
    
    # Historical/factual (static knowledge)
    "when was", "when were", "when did", "when is", "when are",
    "where is", "where are", "where was", "where were", "where did",
    "invented", "created", "discovered", "founded", "established",
    "born", "died", "lived", "wrote", "painted", "composed",
    
    # Conceptual/theoretical
    "theory", "theories", "theoretical", "concept", "concepts", "conceptual",
    "principle", "principles", "law", "laws", "rule", "rules",
    "formula", "formulas", "formulae", "equation", "equations",
]

# Exclusion patterns - questions that definitely DON'T need web search
# These are comprehensive patterns that catch general knowledge questions
EXCLUSION_PATTERNS = [
    # Definition/meaning patterns
    r"what (is|are|was|were) (the |a |an )?(definition|meaning|concept|idea|theory|principle|law|rule|formula|equation|term|word)",
    r"explain (the |a |an )?(concept|idea|theory|principle|law|rule|formula|equation|term|meaning)",
    r"define (the |a |an )?(.*)",
    r"what (does|do|did) (.*?) (mean|means|meant)",
    r"meaning of (.*)",
    
    # How things work (processes, mechanisms)
    r"how (does|do|did|to) (.*?)(work|function|operate|run|process)",
    r"how (is|are|was|were) (.*?)(made|created|built|produced|manufactured)",
    r"how (does|do|did) (.*?)(work|function)",
    r"mechanism (of|for) (.*)",
    r"process (of|for) (.*)",
    
    # Comparison patterns
    r"what (is|are) (the |a |an )?(difference|differences|similarity|similarities|distinction) (between|among)",
    r"compare (.*?) (and|with|to|versus|vs) (.*)",
    r"(difference|similarity|comparison) (between|among|of) (.*)",
    r"(.*?) (vs|versus|compared to|compared with) (.*)",
    
    # Why questions (conceptual)
    r"why (is|are|was|were|did|does|do) (.*?)(important|significant|relevant|necessary|needed)",
    r"why (does|do|did) (.*?)(happen|occur|exist|work|function)",
    r"reason (why|for|behind) (.*)",
    r"what (is|are) (the |a |an )?reason (for|why|behind)",
    
    # Example/listing patterns
    r"give (me |us )?(an |a )?example (of|for)",
    r"list (the |some |a few |all )?(.*)",
    r"what (are|is) (the |some |a few )?(types|kinds|categories|examples|varieties) (of|for)",
    r"name (some |a few |all |the )?(.*)",
    r"enumerate (.*)",
    
    # Measurement questions (static facts)
    r"how (much|many|long|far|old|fast|tall|wide|deep|heavy|big|large|small) (is|are|was|were) (the |a |an )?(.*?)(\?|$)",
    r"what (is|are) (the |a |an )?(size|length|width|height|weight|mass|volume|area|distance|speed) (of|for)",
    
    # Composition/material questions
    r"what (is|are) (.*?)(made of|composed of|consists of|contains|includes)",
    r"composition (of|for) (.*)",
    r"ingredients (of|in|for) (.*)",
    
    # Historical facts (static)
    r"when (was|were|did) (.*?)(invented|created|discovered|founded|born|died|established|built)",
    r"who (invented|created|discovered|founded|wrote|painted|composed|designed|built|made)",
    r"where (is|are|was|were) (.*?)(located|found|from|situated)",
    r"when (was|were) (.*?)(born|died|born in|died in)",
    
    # Scientific/mathematical concepts
    r"what (is|are) (the |a |an )?(formula|equation|theorem|law|principle) (for|of)",
    r"calculate (.*)",
    r"solve (.*)",
    r"formula (for|of) (.*)",
    
    # Language/grammar
    r"what (does|do|did) (.*?) (mean|means)",
    r"spelling (of|for) (.*)",
    r"pronunciation (of|for) (.*)",
    r"grammar (of|for) (.*)",
    
    # Philosophical/conceptual
    r"what (is|are) (the |a |an )?(philosophy|concept|idea|theory) (of|behind|about)",
    r"explain (the |a |an )?(philosophy|concept|idea|theory) (of|behind|about)",
]

# Patterns that indicate CURRENT/REAL-TIME information is needed
# These are high-confidence patterns that strongly indicate web search is necessary
REAL_TIME_PATTERNS = [
    # Price/value queries with time indicators
    r"(what|how much|what is|what's) (the |a )?(current|latest|today|now|right now|as of) (.*?)(price|cost|value|rate|worth|valuation)",
    r"(.*?) (current|latest|today|now|right now|as of) (price|cost|value|rate|worth|valuation|pricing)",
    r"(how much|what) (is|are|was|were) (.*?)(worth|costing|priced|valued) (today|now|right now|currently|at the moment)",
    r"(what|tell me) (is|are) (the |a )?(current|latest|today) (.*?)(price|cost|value)",
    
    # News and updates
    r"(what|tell me|show me|give me) (the |some |any )?(latest|recent|current|today|breaking|just) (news|updates|information|events|happening|developments)",
    r"(what|who|when|where) (.*?)(happened|occurred|announced|released|published|launched) (today|yesterday|recently|this week|this month|just now)",
    r"(breaking|latest|recent|current) (news|update|announcement|event|development) (about|on|regarding)",
    r"(what|tell me) (is|are) (the |some )?(headlines|stories|articles) (today|now|recently)",
    
    # Time-sensitive scheduling
    r"(when|what time) (is|are|was|were) (.*?)(today|now|right now|happening|scheduled|starting|ending)",
    r"(what|tell me) (is|are) (happening|going on|scheduled) (today|now|right now|this week|this month)",
    r"(when|what time) (does|do|did|will) (.*?)(start|begin|end|happen|occur) (today|now|tomorrow)",
    
    # Financial/market data
    r"(.*?) (stock|share|bitcoin|btc|ethereum|eth|crypto|cryptocurrency|currency|forex) (price|value|worth|trading at)",
    r"(what|how much) (is|are) (.*?) (stock|share|crypto|bitcoin|ethereum) (price|value|worth)",
    r"(market|trading|exchange) (price|value|rate|status) (of|for)",
    r"(nasdaq|dow|s&p|sp500|ftse|nikkei) (.*?)(today|now|current)",
    
    # Weather queries
    r"(what|tell me) (is|are) (the |some )?(weather|forecast|temperature|conditions) (today|now|right now|tomorrow|this week)",
    r"(weather|forecast|temperature) (today|now|right now|tomorrow|this week|for)",
    r"(how|what) (is|are) (the |some )?(weather|temperature) (like|in) (.*?)(today|now)",
    
    # Sports scores/results
    r"(what|tell me) (is|are) (the |some )?(score|scores|result|results) (of|for)",
    r"(who|which) (won|wins|winning|lost|loses|losing) (.*?)(game|match|tournament|championship)",
    r"(score|result) (of|for) (.*?)(game|match|tournament)",
    
    # Current status/state
    r"(what|tell me) (is|are) (the |a )?(current|latest|today) (status|state|condition|situation) (of|for)",
    r"(current|latest|today) (status|state|condition|situation) (of|for)",
    r"(how|what) (is|are) (.*?)(doing|going|performing) (today|now|currently)",
    
    # Technology releases/updates
    r"(what|tell me) (is|are) (the |some )?(latest|new|recent|current) (version|release|update|patch) (of|for)",
    r"(latest|new|recent|current) (version|release|update|patch) (of|for)",
    r"(when|what time) (was|is|will) (.*?)(released|launched|announced) (today|recently|this week)",
    
    # Business/company current info
    r"(what|tell me) (is|are) (.*?)(earnings|revenue|profit|stock price) (today|now|this quarter|this year)",
    r"(current|latest|today) (earnings|revenue|profit|stock price) (of|for)",
    
    # Travel/transportation
    r"(what|tell me) (is|are) (the |some )?(flight|flights|delays|cancellations) (today|now|for)",
    r"(flight|traffic|road conditions) (status|delays|cancellations) (today|now|for)",
    
    # Health/medical alerts
    r"(what|tell me) (is|are) (the |some )?(latest|current|recent) (outbreak|cases|deaths|health alert)",
    r"(current|latest|recent) (outbreak|cases|deaths|health alert) (of|for|regarding)",
]

def needs_web_search(message: str, conversation_context: Optional[List[Dict[str, str]]] = None) -> bool:
    """
    Decision matrix to determine if a message requires web search for current/accurate information.
    
    This function uses a scoring-based decision matrix to intelligently determine when web search
    is needed, avoiding false positives and unnecessary API calls.
    
    DECISION MATRIX SCORING:
    ------------------------
    +3 points: Real-time patterns (e.g., "current price", "latest news", "today's weather")
    +2 points: Strong indicators (time-sensitive words, financial data, news, weather)
    +1 point:  Domain-specific patterns (crypto, stocks, sports scores)
    -2 points: General knowledge indicators (definitions, explanations, how-to)
    -5 points: Exclusion patterns (immediate rejection - e.g., "what is gravity?")
    
    THRESHOLD: Requires score >= 2 to trigger web search
    
    EXAMPLES:
    ---------
    ✓ "what's the price of bitcoin right now?" → Score: +3 (real-time) +2 (price) +1 (crypto) = 6 → TRIGGER
    ✓ "what's the weather today?" → Score: +3 (real-time) +2 (weather) = 5 → TRIGGER
    ✗ "what is gravity?" → Score: -5 (exclusion pattern) → NO TRIGGER
    ✗ "explain how photosynthesis works" → Score: -2 (general knowledge) → NO TRIGGER
    ✓ "latest news about AI" → Score: +3 (real-time) +2 (news) = 5 → TRIGGER
    
    Args:
        message: User's message text
        conversation_context: Optional list of previous messages for context (future use)
        
    Returns:
        True if web search is needed, False otherwise
    """
    # Basic validation
    if not message or not isinstance(message, str):
        return False
    
    message_clean = message.strip()
    if len(message_clean) < 10:
        return False
    
    message_lower = message_clean.lower()
    score = 0
    exclusion_matched = False
    time_sensitive_present = False
    
    # Check for time-sensitive words early (used in multiple checks)
    time_sensitive_words = ["current", "latest", "today", "now", "recent", "recently", "right now", "as of", "this week", "this month", "this year"]
    time_sensitive_present = any(time_word in message_lower for time_word in time_sensitive_words)
    
    # EXCLUSION CHECK: If it matches exclusion patterns, immediately reject
    for pattern in EXCLUSION_PATTERNS:
        if re.search(pattern, message_lower):
            # BUT: If time-sensitive words are present, don't exclude (e.g., "what is the current definition" might need search)
            if not time_sensitive_present:
                logger.debug(f"Web search excluded by pattern: {pattern}")
                return False
            else:
                exclusion_matched = True
                logger.debug(f"Exclusion pattern matched but time-sensitive words present, continuing evaluation")
                break
    
    # Check for general knowledge indicators (negative score)
    # But only apply penalty if NO time-sensitive words are present
    general_knowledge_penalty = 0
    for indicator in GENERAL_KNOWLEDGE_INDICATORS:
        if indicator in message_lower:
            # Only penalize if NOT combined with time-sensitive words
            if not time_sensitive_present:
                general_knowledge_penalty += 2
                logger.debug(f"General knowledge indicator found: {indicator}")
    
    if general_knowledge_penalty > 0:
        score -= min(general_knowledge_penalty, 4)  # Cap penalty at -4
        logger.debug(f"General knowledge penalty applied: {general_knowledge_penalty} (score: {score})")
    
    # Check for strong web search indicators
    for indicator in STRONG_WEB_SEARCH_INDICATORS:
        if indicator in message_lower:
            score += 2
            logger.debug(f"Strong web search indicator found: {indicator} (score: {score})")
    
    # Check for real-time patterns (highest priority)
    for pattern in REAL_TIME_PATTERNS:
        if re.search(pattern, message_lower):
            score += 3
            logger.debug(f"Real-time pattern matched: {pattern} (score: {score})")
            break  # One match is enough
    
    # Check for question words combined with time-sensitive terms
    question_time_pattern = r"(what|who|when|where|how much|how many) (is|are|was|were) (.*?)(current|latest|today|now|recent|recently)"
    if re.search(question_time_pattern, message_lower):
        score += 2
        logger.debug(f"Question + time-sensitive pattern matched (score: {score})")
    
    # Check for specific domains that always need current info (comprehensive)
    domain_patterns = [
        # Financial/crypto
        r"(bitcoin|btc|ethereum|eth|crypto|cryptocurrency|altcoin|altcoins|stock|share|shares|nasdaq|dow|s&p|sp500|ftse|nikkei|dax|cac|forex|fx)",
        # Weather
        r"(weather|forecast|temperature|temp|temperatures|rain|snow|wind|humidity|precipitation|storm|storms|hurricane|tornado)",
        # News/events
        r"(news|breaking|update|updates|announcement|announcements|headline|headlines|report|reports|story|stories)",
        # Sports
        r"(score|scores|game|games|match|matches|result|results|tournament|tournaments|championship|championships|league|leagues)",
        # Technology releases
        r"(version|versions|release|releases|update|updates|patch|patches|beta|alpha|preview)",
        # Business/company
        r"(earnings|revenue|profit|quarterly|annual report|ipo|merger|acquisition|stock split|dividend|dividends)",
        # Travel
        r"(flight|flights|airline|airlines|delay|delays|cancelled|cancellations|traffic|road conditions|transit)",
        # Health/medical
        r"(outbreak|pandemic|epidemic|cases|deaths|vaccine|vaccines|health alert|medical emergency)",
        # Politics/elections
        r"(election|elections|vote|votes|voting|poll|polls|polling|president|presidential|senate|congress|parliament)",
    ]
    for pattern in domain_patterns:
        if re.search(pattern, message_lower):
            score += 1
            logger.debug(f"Domain pattern matched: {pattern} (score: {score})")
    
    # Additional scoring for combinations that strongly indicate current info needed
    # Question word + time indicator + domain
    question_time_domain = r"(what|who|when|where|how much|how many) (is|are|was|were) (the |a )?(current|latest|today|now|recent) (.*?)(price|news|weather|score|update|status)"
    if re.search(question_time_domain, message_lower):
        score += 2
        logger.debug(f"Question + time + domain pattern matched (score: {score})")
    
    # Imperative requests for current info
    imperative_patterns = [
        r"(tell me|show me|give me|find|search for|look up) (the |some |any )?(current|latest|today|now|recent) (.*)",
        r"(check|get|fetch) (the |some |any )?(current|latest|today|now|recent) (.*)",
    ]
    for pattern in imperative_patterns:
        if re.search(pattern, message_lower):
            score += 1
            logger.debug(f"Imperative pattern matched: {pattern} (score: {score})")
            break
    
    # Edge case: Very short messages with strong indicators might be false positives
    # Require slightly higher threshold for very short messages
    if len(message_clean) < 20 and score < 3:
        logger.debug(f"Short message with low score, not triggering (score: {score}, length: {len(message_clean)})")
        return False
    
    # Edge case: If exclusion was matched but we continued, require higher score
    if exclusion_matched and score < 3:
        logger.debug(f"Exclusion matched but time-sensitive present, requiring higher score (score: {score})")
        return False
    
    # Final decision: Need at least 2 points to trigger web search
    # This prevents false positives from simple questions
    # But if real-time pattern matched, lower threshold to 1 (very high confidence)
    real_time_matched = any(re.search(pattern, message_lower) for pattern in REAL_TIME_PATTERNS)
    threshold = 1 if real_time_matched else 2
    
    decision = score >= threshold
    
    if decision:
        logger.info(f"✓ Web search TRIGGERED (score: {score}, threshold: {threshold}) for: {message_clean[:100]}")
    else:
        logger.debug(f"✗ Web search NOT triggered (score: {score}, threshold: {threshold}) for: {message_clean[:100]}")
    
    return decision

def extract_search_query(message: str) -> str:
    """
    Extract a search query from a user message
    
    Args:
        message: User's message text
        
    Returns:
        Cleaned search query string
    """
    # Remove common prefixes
    message = re.sub(r"^(can you|please|could you|would you|tell me|show me|find|search for|look up)\s+", "", message, flags=re.IGNORECASE)
    
    # Remove question marks and clean up
    message = message.strip("?.,!").strip()
    
    # Limit length
    if len(message) > 200:
        message = message[:200]
    
    return message

def format_search_results_naturally(results: List[Dict[str, Any]], query: str) -> str:
    """
    Format search results as natural, flowing prose - like a human would write it
    Not structured data, not JSON, not bullet points - just natural sentences
    
    Args:
        results: List of search result dicts
        query: Original search query
        
    Returns:
        Formatted natural text
    """
    if not results:
        return ""
    
    natural_context_parts = []
    
    for result in results[:5]:  # Limit to 5 results
        title = result.get("title", "").strip()
        content = result.get("content", "").strip()
        
        if not content:
            continue
        
        # Clean up content to be more readable - remove markdown, brackets, etc.
        clean_content = content
        # Remove markdown links but keep text: [text](url) -> text
        clean_content = re.sub(r'\[([^\]]+)\]\([^\)]+\)', r'\1', clean_content)
        # Remove standalone brackets: [text] -> text  
        clean_content = re.sub(r'\[([^\]]+)\]', r'\1', clean_content)
        # Remove excessive whitespace and newlines
        clean_content = re.sub(r'\s+', ' ', clean_content)
        clean_content = clean_content.strip()
        
        # Limit length but try to end at a sentence
        if len(clean_content) > 350:
            truncated = clean_content[:350]
            last_period = truncated.rfind('.')
            last_question = truncated.rfind('?')
            last_exclamation = truncated.rfind('!')
            cut_point = max(last_period, last_question, last_exclamation)
            if cut_point > 150:
                clean_content = clean_content[:cut_point + 1]
            else:
                clean_content = clean_content[:350] + "..."
        
        # Format as natural sentence(s) - write it like a human would
        if title and title.lower() not in clean_content.lower()[:50]:
            # Only add title if it's not already in the content
            natural_text = f"According to {title}, {clean_content}"
        else:
            natural_text = clean_content
        
        # Ensure it ends with punctuation
        if natural_text and not natural_text[-1] in '.!?':
            natural_text += "."
        
        if natural_text.strip():
            natural_context_parts.append(natural_text.strip())
    
    if not natural_context_parts:
        return ""
    
    # Combine into flowing paragraphs - write it like a human assistant would present information
    # Use natural transitions between sources, not structured lists or numbered items
    if len(natural_context_parts) == 1:
        formatted = natural_context_parts[0]
    else:
        # Add natural transitions between multiple sources
        formatted = natural_context_parts[0]
        for part in natural_context_parts[1:]:
            # Add a natural transition - like "Additionally," or "Furthermore," or just continue
            formatted += " " + part
    
    return formatted

