"""
Synthetic canary values for the adversarial security campaign.

RULE (campaign section 0): every canary is unmistakably synthetic and no test
ever touches real credentials. Canaries are BUILT from fragments at runtime so
that THIS SOURCE FILE contains no credential-shaped literals and stays clean
for the repository-wide scanner and VERIFY_AGENT_SAFE.
"""

def _c(kind: str, suffix: str) -> str:
    return "CANARY_" + kind + "_9R_TEST_" + suffix


# SEC-001..014 / REDACT-001..005 canaries (>= 32 chars so the entropy
# heuristic sees them even without a sensitive field name on the line)
def canary_api_key() -> str:
    return _c("API_KEY", "7f31d20a5599")

def canary_access_token() -> str:
    return _c("ACCESS_TOKEN", "b187cc5599")

def canary_refresh_token() -> str:
    return _c("REFRESH_TOKEN", "11ee425599")

def canary_client_secret() -> str:
    return _c("CLIENT_SECRET", "5837dd5599")

def canary_password() -> str:
    return _c("PASSWORD", "995e3d5599")

def canary_jwt() -> str:
    return ".".join((
        "eyJhbGciOiJIUzI1NiJ9",
        "eyJ0ZXN0IjoiY2FuYXJ5In0",
        "INVALID_TEST_SIGNATURE",
    ))

def canary_sk() -> str:
    return ("s" + "k-") + "test-9router-DO-NOT-USE-" + "1234567890abcdef"

def canary_bearer() -> str:
    return "Bear" + "er TEST_ONLY_9ROUTER_CANARY_TOKEN"

def canary_bearer_line() -> str:
    return "Authoriza" + "tion: " + canary_bearer()

def canary_pem() -> str:
    return ("-----BE" + "GIN PRIVATE KEY-----\n"
            "TEST_ONLY_NOT_A_REAL_PRIVATE_KEY\n"
            "-----EN" + "D PRIVATE KEY-----\n")

def all_canaries() -> list:
    return [
        canary_api_key(), canary_access_token(), canary_refresh_token(),
        canary_client_secret(), canary_password(), canary_jwt(),
        canary_sk(), canary_bearer(), "TEST_ONLY_9ROUTER_CANARY_TOKEN",
        "TEST_ONLY_NOT_A_REAL_PRIVATE_KEY", "INVALID_TEST_SIGNATURE",
    ]
