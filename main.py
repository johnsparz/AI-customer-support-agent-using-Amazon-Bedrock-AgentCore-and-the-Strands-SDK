"""
Customer Support AI Agent — Starter Code
==========================================
Your task is to complete this file by implementing all sections marked
with # TODO comments.

Reference the step-by-step solution files and INSTRUCTIONS.md for guidance.
Do NOT copy the solution directly — work through each section yourself.

Run locally (after filling in config values):
  uv run main.py '{"prompt": "Hello", "customer_id": "CUST-123", "session_id": "s1"}'

Deploy to AgentCore:
  agentcore deploy

Invoke deployed agent:
  agentcore invoke '{"prompt": "Hello", "customer_id": "CUST-123", "session_id": "s1"}'
"""

# ── Imports ───────────────────────────────────────────────────────────────────
# These imports are provided. Do not remove them.
from strands import Agent, tool
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from bedrock_agentcore.memory import MemoryClient
from strands.models import BedrockModel
from strands.tools.mcp.mcp_client import MCPClient
from mcp.client.streamable_http import streamablehttp_client
import argparse, json
import os, asyncio, boto3
from strands.hooks import (
    HookProvider, AfterInvocationEvent, HookRegistry, MessageAddedEvent,
)
import logging
import uuid
from typing import Dict
from bedrock_agentcore.tools.code_interpreter_client import code_session
from strands_tools.browser import AgentCoreBrowser


logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger("CSAI_Agent")

# ── TODO 1 — App Initialisation ───────────────────────────────────────────────
app = BedrockAgentCoreApp()

os.environ["BYPASS_TOOL_CONSENT"] = "true"


# ── TODO 2 — Configuration ────────────────────────────────────────────────────
GATEWAY_URL = "https://customersupportgateway-id9qz0w6an.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp"   # TODO: Replace with your Gateway URL
KB_ID       = "PZ9Z091BTS"          # TODO: Replace with your Knowledge Base ID
REGION      = "us-east-1"        # TODO: Replace with your AWS region
MEMORY_ID   = "CustomerSupportMemory-DkUX3qFGbb"        # TODO: Replace with your Memory ID

SYSTEM_PROMPT = "You are a helpful customer support agent for Amazon."


# ── TODO 3 — Model and Clients ────────────────────────────────────────────────
model_id = "global.amazon.nova-2-lite-v1:0"

# TODO: Create the BedrockModel instance
model = BedrockModel(model_id=model_id)

# TODO: Create the MemoryClient instance
memory_client = MemoryClient(region_name=REGION)

# TODO: Create the boto3 bedrock-agent-runtime client
_bedrock_runtime = boto3.client("bedrock-agent-runtime", region_name=REGION)

def _get_gateway_token() -> str:
    """Return a bearer token for the AgentCore Gateway, or "" if unavailable."""
    token = os.environ.get("GATEWAY_ACCESS_TOKEN", "").strip()
    if token:
        return token

    token_endpoint = os.environ.get("COGNITO_TOKEN_ENDPOINT", "").strip()
    client_id = os.environ.get("GATEWAY_CLIENT_ID", "").strip()
    client_secret = os.environ.get("GATEWAY_CLIENT_SECRET", "").strip()
    if not (token_endpoint and client_id and client_secret):
        logger.warning("No Gateway credentials found; Gateway tools will not load.")
        return ""

    try:
        import urllib.parse
        import urllib.request

        body = urllib.parse.urlencode({
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        }).encode()
        req = urllib.request.Request(
            token_endpoint,
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read()).get("access_token", "")
    except Exception as e:
        logger.warning("Failed to fetch Gateway token: %s", e)
        return ""


def _create_gateway_transport():
    """Build the streamable HTTP transport used by MCPClient."""
    headers = {}
    token = _get_gateway_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return streamablehttp_client(GATEWAY_URL, headers=headers)


# ── TODO 4 — Namespace Helper ─────────────────────────────────────────────────
def get_namespaces(mem_client: MemoryClient, memory_id: str) -> Dict:
    """Return a dict mapping strategy type → namespace template string."""
    # TODO: Implement this function
    # FIX: guard the API call. If it raises, MemoryHook.__init__ dies and takes
    # the whole request with it instead of degrading to "no memory".
    try:
        strategies = mem_client.get_memory_strategies(memory_id)
        return {s["type"]: s["namespaces"][0] for s in strategies}
    except Exception as e:
        logger.warning("Failed to load memory strategies: %s", e)
        return {}

# ── TODO 5 — Memory Hook ──────────────────────────────────────────────────────
class MemoryHook(HookProvider):
    """Long-term memory hook for the customer support agent."""

    def __init__(
        self,
        actor_id: str,
        session_id: str,
        memory_client: MemoryClient,
        memory_id: str,
    ):
        # TODO: Store actor_id, session_id, memory_id, memory_client as attributes
        self.actor_id = actor_id
        self.session_id = session_id
        self.memory_client = memory_client
        self.memory_id = memory_id

        # TODO: Call get_namespaces() and store the result as self.namespaces
        self.namespaces = get_namespaces(self.memory_client, self.memory_id)

        # FIX: remember the customer's real question. Without this, the text we
        # inject as "Customer Context:" gets written back into memory on save,
        # so retrieved memories compound into the stored record every turn.
        self._last_user_query = None

    def retrieve_customer_context(self, event: MessageAddedEvent):
        """Retrieve relevant memories and prepend them to the user message."""
        # TODO: Implement memory retrieval
        # Steps:
        #   1. Get the last message from event.agent.messages
        #   2. Check it is a user message and not a tool result
        #   3. Extract the user query text
        #   4. For each namespace in self.namespaces, call retrieve_memories()
        #   5. Collect non-empty memory texts with strategy type tags
        #   6. If any found, prepend them to the user message

        # 1. Get the last message from event.agent.messages
        if not event.agent.messages:
            return
        msg = event.agent.messages[-1]

        # Helper to handle both dictionaries and objects depending on the framework's message type
        is_dict = isinstance(msg, dict)
        role = msg.get("role") if is_dict else getattr(msg, "role", None)
        content = msg.get("content", []) if is_dict else getattr(msg, "content", [])

        # 2. Check it is a user message
        if role != "user" or not content:
            return

        first_block = content[0]
        is_block_dict = isinstance(first_block, dict)

        # Check it is plain text and not a tool result
        if is_block_dict and "text" not in first_block:
            return
        if not is_block_dict and not hasattr(first_block, "text"):
            return

        # 3. Extract the user query text
        query = first_block["text"] if is_block_dict else first_block.text

        # FIX: never re-inject context into a message we already decorated.
        if query.startswith("Customer Context:"):
            return
        self._last_user_query = query

        # 4 & 5. For each namespace, retrieve memories and collect tagged texts
        # FIX: wrap in try/except so a memory-service hiccup does not abort the turn.
        collected_memories = []
        try:
            for strategy, ns_template in self.namespaces.items():
                namespace = ns_template.format(actorId=self.actor_id)
                memories = self.memory_client.retrieve_memories(
                    memory_id=self.memory_id,
                    namespace=namespace,
                    query=query,
                    top_k=5,
                )
                for m in memories:
                    # FIX: RetrieveMemoryRecords returns memoryRecordSummary items
                    # shaped {"content": {"text": ...}, "score": ...}. Reading
                    # m["text"] always came back empty, so no memory was ever
                    # injected and cross-session recall (Test 4) could not pass.
                    if isinstance(m, dict):
                        record = m.get("content", {})
                        text_val = record.get("text", "") if isinstance(record, dict) else str(record)
                    else:
                        record = getattr(m, "content", None)
                        text_val = getattr(record, "text", "") if record is not None else ""
                    text_val = (text_val or "").strip()
                    if text_val:
                        collected_memories.append(f"[{strategy}] {text_val}")
        except Exception as e:
            logger.warning("Failed to retrieve customer context: %s", e)
            return

        # 6. If any found, prepend them to the user message
        if collected_memories:
            context_str = "\n".join(collected_memories)
            new_text = f"Customer Context:\n{context_str}\n\n{query}"

            if is_block_dict:
                first_block["text"] = new_text
            else:
                first_block.text = new_text

    def save_support_interaction(self, event: AfterInvocationEvent):
        """Save the completed turn to memory after the agent responds."""
        # TODO: Implement memory saving
        user_query = None
        assistant_response = None

        # 1 & 2. Walk backwards to find the last plain-text user query and assistant response
        for msg in reversed(event.agent.messages):
            is_dict = isinstance(msg, dict)
            role = msg.get("role") if is_dict else getattr(msg, "role", None)
            content = msg.get("content", []) if is_dict else getattr(msg, "content", [])

            if not content:
                continue

            first_block = content[0]
            is_block_dict = isinstance(first_block, dict)

            # Helper to extract text if it exists
            has_text = ("text" in first_block) if is_block_dict else hasattr(first_block, "text")
            extracted_text = first_block["text"] if (has_text and is_block_dict) else (first_block.text if has_text else None)

            if role == "assistant" and not assistant_response and extracted_text:
                assistant_response = extracted_text
            elif role == "user" and not user_query and extracted_text:
                user_query = extracted_text

            if user_query and assistant_response:
                break

        if self._last_user_query:
            user_query = self._last_user_query

        if user_query and assistant_response:
            try:
                self.memory_client.create_event(
                    memory_id=self.memory_id,
                    actor_id=self.actor_id,
                    session_id=self.session_id,
                    messages=[(user_query, "USER"), (assistant_response, "ASSISTANT")],
                )
            except Exception as e:
                logger.warning("Failed to save support interaction: %s", e)

    def register_hooks(self, registry: HookRegistry) -> None:  # type: ignore
        """Register both memory callbacks."""
        # TODO: Register retrieve_customer_context on MessageAddedEvent
        registry.add_callback(MessageAddedEvent, self.retrieve_customer_context)
        # TODO: Register save_support_interaction on AfterInvocationEvent
        registry.add_callback(AfterInvocationEvent, self.save_support_interaction)


# ── TODO 6 — Knowledge Base Tool ─────────────────────────────────────────────
@tool
def search_knowledge_base(query: str) -> str:
    """
    Search the Amazon product catalog and support knowledge base.
    Use this for product specifications, return policies, warranty
    information, loyalty program details, and order status definitions.

    Args:
        query: The question or topic to search for

    Returns:
        Relevant information retrieved from the knowledge base
    """
    # TODO: Implement the Knowledge Base search

    # 1. Guard: if KB_ID is empty
    if not KB_ID or KB_ID.startswith("<"):
        return "Knowledge base not configured."

    # 2. Call _bedrock_runtime.retrieve
   
    try:
        response = _bedrock_runtime.retrieve(
            knowledgeBaseId=KB_ID,
            retrievalQuery={"text": query}
        )
    except Exception as e:
        logger.warning("Knowledge base retrieve failed: %s", e)
        return f"Knowledge base search failed: {e}"

    # 3. Extract retrievalResults and handle empty case
    results = response.get("retrievalResults", [])
    if not results:
        return "No relevant information found in the knowledge base."

    # 4. Join the text chunks and return
    text_chunks = [
        r.get("content", {}).get("text", "")
        for r in results
        if r.get("content", {}).get("text")
    ]
    if not text_chunks:
        return "No relevant information found in the knowledge base."
    return "\n---\n".join(text_chunks)


# ── TODO 7 — Loyalty Discount Tool (Code Interpreter) ────────────────────────

@tool
def calculate_loyalty_discount(
    loyalty_points: int,
    tier: str,
    order_total: float,
    product_category: str = "standard",
) -> str:
    """
    Calculate the loyalty discount for a customer order using the
    AgentCore Code Interpreter. Runs exact arithmetic in a secure sandbox.

    Args:
        loyalty_points:   Customer's current points balance
        tier:             Customer tier — Silver, Gold, or Platinum
        order_total:      Order total in USD
        product_category: standard, device, or fresh

    Returns:
        Full discount breakdown and final price
    """
    # TODO: Build the code string (use an f-string to inject the arguments)
    code = f"""import json

earn_rates = {{"standard": 1, "device": 2, "fresh": 5}}
tier_rates = {{"Silver": 0.00, "Gold": 0.10, "Platinum": 0.15}}

loyalty_points = {loyalty_points}
tier = "{tier}"
order_total = {order_total}
product_category = "{product_category}"

# 1. Points redemption (100 points = $1, floored to nearest 500, capped at 50% order total)
max_discount = order_total * 0.5
max_points_allowed = int(max_discount * 100)
usable_points = (loyalty_points // 500) * 500
points_redeemed = min(usable_points, (max_points_allowed // 500) * 500)

point_discount = points_redeemed / 100.0
subtotal_after_points = order_total - point_discount

# 2. Tier discount applied to subtotal after points
tier_rate = tier_rates.get(tier, 0.0)
tier_discount = subtotal_after_points * tier_rate

# 3. Final calculations
final_total = subtotal_after_points - tier_discount
total_savings = point_discount + tier_discount
remaining_points = loyalty_points - points_redeemed
earn_rate = earn_rates.get(product_category, 1)
points_earned = int(final_total * earn_rate)

result = {{
    "order_total": order_total,
    "points_redeemed": points_redeemed,
    "point_discount": round(point_discount, 2),
    "tier": tier,
    "tier_discount_pct": round(tier_rate * 100, 2),
    "tier_discount": round(tier_discount, 2),
    "final_total": round(final_total, 2),
    "total_savings": round(total_savings, 2),
    "remaining_points": remaining_points,
    "points_earned": points_earned
}}

print(json.dumps(result))
"""

    try:
        with code_session(REGION) as code_client:
            response = code_client.invoke(
                "executeCode",
                {"code": code, "language": "python", "clearContext": True},
            )
            for event in response["stream"]:
                return json.dumps(event["result"])
        raise RuntimeError("Code Interpreter returned no result events.")
    except Exception as e:
        # TODO: Implement fallback calculation using tier discount only
        tier_rates = {"Silver": 0.00, "Gold": 0.10, "Platinum": 0.15}
        tier_rate = tier_rates.get(tier, 0.0)
        tier_discount = order_total * tier_rate
        final_total = order_total - tier_discount
        return json.dumps({
            "order_total": order_total,
            "points_redeemed": 0,
            "tier": tier,
            "tier_discount_pct": round(tier_rate * 100, 2),
            "tier_discount": round(tier_discount, 2),
            "final_total": round(final_total, 2),
            "remaining_points": loyalty_points,
            "note": f"Fallback calculation used (Code Interpreter unavailable: {str(e)})"
        })


# ── TODO 8 — Agent Entrypoint ─────────────────────────────────────────────────

@app.entrypoint
async def invoke(payload, context=None):
    """
    Main handler called by AgentCore for every incoming request.

    Expected payload keys:
      prompt      (str, required) — the customer's message
      customer_id (str, optional) — unique customer identifier
      session_id  (str, optional) — session identifier; generated if absent
    """
    # TODO: Implement the agent invocation
    try:
      
        if isinstance(payload, (str, bytes)):
            payload = json.loads(payload)
        payload = payload or {}

        user_input = payload.get("prompt", "")
        actor_id = payload.get("customer_id", "default_user")
        session_id = payload.get("session_id") or str(uuid.uuid4())

        if not user_input:
            return "No prompt provided. Send {\"prompt\": \"...\"}."

        memory_hook = MemoryHook(
            actor_id=actor_id,
            session_id=session_id,
            memory_client=memory_client,
            memory_id=MEMORY_ID,
        )

    
        agent_core_browser = AgentCoreBrowser(region=REGION)

        tools = [
            search_knowledge_base,
            calculate_loyalty_discount,
            agent_core_browser.browser,
        ]

      
        mcp_client = MCPClient(_create_gateway_transport)

        with mcp_client:
            try:
                gateway_tools = mcp_client.list_tools_sync()
                tools.extend(gateway_tools)
                logger.info("Loaded %d Gateway tools.", len(gateway_tools))
            except Exception as e:
                logger.warning("Failed to load Gateway tools: %s", e)

          
            agent = Agent(
                model=model,
                tools=tools,
                hooks=[memory_hook],
                system_prompt=SYSTEM_PROMPT,
            )
   
            response = await agent.invoke_async(user_input)

    
        message = getattr(response, "message", None)
        if isinstance(message, dict):
            content = message.get("content") or []
            for block in content:
                text = block.get("text") if isinstance(block, dict) else getattr(block, "text", None)
                if text:
                    return text
        elif message is not None:
            content = getattr(message, "content", None) or []
            for block in content:
                text = block.get("text") if isinstance(block, dict) else getattr(block, "text", None)
                if text:
                    return text
        return str(response)

    except Exception as e:
        
        logger.exception("Invocation failed")
        return f"An error occurred while processing your request: {str(e)}"

# ── CLI entry point (do not modify) ──────────────────────────────────────────
def main():
    """Run one invocation from the command line for local testing."""
    parser = argparse.ArgumentParser()
    parser.add_argument("payload", type=str)
    args = parser.parse_args()
    response = asyncio.run(invoke(json.loads(args.payload)))
    print(response)


if __name__ == "__main__":
    app.run()
    # Uncomment the line below and comment app.run() for local CLI testing:
    #main()
