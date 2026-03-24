"""Read-only CLOB smoke test (no API keys). Safe to keep in-repo; do not add credentials."""
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds
from decimal import Decimal
import os

def diag():
    # Use public client (no creds) to see if we can read order books
    client = ClobClient(
        host="https://clob.polymarket.com",
        chain_id=137,
        signature_type=2,
    )
    
    # Example token ID for a BTC 5m market (hardcoded just for test if possible, or fetch one)
    # Better yet, let's just try to fetch 1 market and its tokens
    print("Fetching markets...")
    try:
        # Use simple requests to find a token id if client is restricted
        import requests
        resp = requests.get("https://gamma-api.polymarket.com/markets?tag=5M&active=true&limit=1")
        markets = resp.json()
        if not markets:
            print("No active 5M markets found.")
            return
        
        m = markets[0];
        tokens = m.get('clobTokenIds')
        if isinstance(tokens, str):
            import json
            tokens = json.loads(tokens)
        
        token_id = tokens[0]
        print(f"Testing Order Book for Token: {token_id} ({m.get('question')})")
        
        book = client.get_order_book(token_id)
        if book.asks:
            print(f"SUCCESS: Best Ask: {book.asks[0].price}")
        if book.bids:
            print(f"SUCCESS: Best Bid: {book.bids[0].price}")
        if not book.asks and not book.bids:
            print("FAILURE: Book is empty.")
            
    except Exception as e:
        print(f"ERROR: {str(e)}")

if __name__ == "__main__":
    diag()
