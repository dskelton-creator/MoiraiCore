"""
Threat intelligence knowledge base for auto-assessment from natural language descriptions.
Structured so the engine can infer data flows, trust boundaries, threats, and controls
from a free-form system description without manual modelling.
"""

# ── System type patterns → inferred architecture ──────────────────────
SYSTEM_PATTERNS = {
    "web_app": {
        "keywords": ["web application", "web app", "website", "portal", "dashboard", "saas", "web platform", "online platform", "web-based", "browser-based"],
        "default_data_flows": [
            {"name": "User → Application", "source": "Browser/Client", "destination": "Web Server", "protocol": "HTTPS", "data_type": "Credentials", "crosses_boundary": True},
            {"name": "Application → Database", "source": "Web Server", "destination": "Database", "protocol": "SQL/TLS", "data_type": "Structured", "crosses_boundary": False},
            {"name": "User → API", "source": "Browser/Client", "destination": "API Layer", "protocol": "HTTPS/JSON", "data_type": "Structural", "crosses_boundary": True},
        ],
        "default_trust_boundaries": ["Internet → DMZ (Web)", "DMZ → Internal (DB)"],
        "default_entry_points": ["/api/", "/auth/", "/login", "/admin", "/register", "/webhook"],
        "common_assets": ["User credentials", "Session tokens", "Application data", "Configuration"],
        "threat_profile": "public_web",
    },
    "api": {
        "keywords": ["api", "rest", "graphql", "microservice", "backend", "service", "endpoint"],
        "default_data_flows": [
            {"name": "Client → API Gateway", "source": "External Client", "destination": "API Gateway", "protocol": "HTTPS", "data_type": "Structural", "crosses_boundary": True},
            {"name": "API → Service Mesh", "source": "API Gateway", "destination": "Microservices", "protocol": "HTTPS/gRPC", "data_type": "Structural", "crosses_boundary": False},
            {"name": "Service → Database", "source": "Microservice", "destination": "Database", "protocol": "SQL", "data_type": "Structured", "crosses_boundary": False},
        ],
        "default_trust_boundaries": ["External → API Gateway", "Gateway → Internal Services"],
        "default_entry_points": ["/api/v1/", "/graphql", "/health", "/webhook"],
        "common_assets": ["API keys", "Service tokens", "Request/response data"],
        "threat_profile": "api_exposed",
    },
    "mobile": {
        "keywords": ["mobile app", "ios", "android", "app", "iphone", "smartphone"],
        "default_data_flows": [
            {"name": "Mobile App → API", "source": "Mobile Device", "destination": "Backend API", "protocol": "HTTPS", "data_type": "Credentials", "crosses_boundary": True},
            {"name": "API → Backend DB", "source": "Backend API", "destination": "Database", "protocol": "SQL", "data_type": "Structured", "crosses_boundary": False},
        ],
        "default_trust_boundaries": ["Mobile Network → API", "API → Internal"],
        "default_entry_points": ["/api/auth", "/api/sync", "/api/push"],
        "common_assets": ["Device tokens", "User credentials", "Local storage data"],
        "threat_profile": "mobile",
    },
    "desktop": {
        "keywords": ["desktop", "desktop app", "electron", "native app", "client software"],
        "default_data_flows": [
            {"name": "Desktop → Backend", "source": "Desktop Client", "destination": "Backend Server", "protocol": "HTTPS", "data_type": "Structural", "crosses_boundary": True},
        ],
        "default_trust_boundaries": ["User Machine → Internet → Backend"],
        "default_entry_points": ["/api/sync", "/api/auth"],
        "common_assets": ["Local config", "Cached data", "User files"],
        "threat_profile": "desktop",
    },
    "infrastructure": {
        "keywords": ["server", "infrastructure", "cloud", "aws", "azure", "gcp", "vps", "hosting", "network", "vm", "container", "docker", "kubernetes", "k8s"],
        "default_data_flows": [
            {"name": "Admin → Server", "source": "Admin Workstation", "destination": "Server/VM", "protocol": "SSH/HTTPS", "data_type": "Credentials", "crosses_boundary": True},
            {"name": "Server → Database", "source": "Application Server", "destination": "Database", "protocol": "SQL/TLS", "data_type": "Structural", "crosses_boundary": False},
            {"name": "LB → Instances", "source": "Load Balancer", "destination": "Server Instances", "protocol": "HTTPS", "data_type": "Structural", "crosses_boundary": True},
        ],
        "default_trust_boundaries": ["Internet → Load Balancer", "LB → Instances", "Instances → Data Tier"],
        "default_entry_points": ["SSH (22)", "HTTPS (443)", "Admin panel", "API"],
        "common_assets": ["Server credentials", "SSL certificates", "Configuration files", "Logs"],
        "threat_profile": "infrastructure",
    },
    "database": {
        "keywords": ["database", "db", "sql", "postgres", "mysql", "mongodb", "redis", "data store", "data warehouse"],
        "default_data_flows": [
            {"name": "App → Database", "source": "Application", "destination": "Database Server", "protocol": "SQL/NoSQL", "data_type": "Structured", "crosses_boundary": True},
        ],
        "default_trust_boundaries": ["App Tier → Data Tier"],
        "default_entry_points": ["SQL port", "Admin console", "Backup API"],
        "common_assets": ["Database credentials", "Stored data", "Backup files"],
        "threat_profile": "database",
    },
    "iot": {
        "keywords": ["iot", "sensor", "device", "embedded", "firmware", "hardware", "smart device"],
        "default_data_flows": [
            {"name": "Device → Gateway", "source": "IoT Device", "destination": "Gateway/Hub", "protocol": "MQTT/CoAP", "data_type": "Sensor Data", "crosses_boundary": True},
            {"name": "Gateway → Cloud", "source": "Gateway", "destination": "Cloud Platform", "protocol": "HTTPS", "data_type": "Telemetry", "crosses_boundary": True},
        ],
        "default_trust_boundaries": ["Device → Gateway", "Gateway → Cloud"],
        "default_entry_points": ["Device firmware", "MQTT broker", "Cloud API"],
        "common_assets": ["Device credentials", "Firmware", "Telemetry data"],
        "threat_profile": "iot",
    },
}

# ── Domain-specific threat+control overlays ──────────────────────────
DOMAIN_OVERLAY: dict[str, dict] = {
    "healthcare": {
        "keywords": ["health", "medical", "clinic", "patient", "hospital", "doctor", "pharmacy", "healthcare", "wellness", "therapy", "massage", "remedial", "allied health", "physio", "dental"],
        "extra_data_types": ["PHI", "PII", "eHealth Records", "Medicare Data"],
        "extra_assets": ["Patient health records", "Medicare details", "Clinical notes", "Referral data"],
        "extra_threats": [
            {"name": "PHI Data Breach — Patient Records", "category": "I", "threat_source": "external_hacker", "likelihood": 3, "impact": 5, "default_vulns": ["Unencrypted patient data", "Over-permissioned API", "Weak access controls"], "compliance": ["AU Privacy Act", "Notifiable Data Breaches", "APP 11"]},
            {"name": "Insider Data Exfiltration — Clinic Staff", "category": "I", "threat_source": "insider_malicious", "likelihood": 3, "impact": 5, "default_vulns": ["No DLP", "USB export possible", "No data classification"], "compliance": ["APP 8", "ISO 27001 A.8"]},
            {"name": "Ransomware — Clinical Operations", "category": "D", "threat_source": "external_hacker", "likelihood": 4, "impact": 5, "default_vulns": ["Unpatched systems", "No offline backups", "Lateral movement possible"], "compliance": ["Business continuity", "APP 11.2"]},
            {"name": "Medicare Fraud — Billing System", "category": "T", "threat_source": "insider_malicious", "likelihood": 2, "impact": 5, "default_vulns": ["No billing anomaly detection", "Insufficient audit trail"], "compliance": ["Medicare Compliance", "Fraud prevention"]},
        ],
        "regulatory_frameworks": ["Australian Privacy Act 1988", "APPs (13 principles)", "Notifiable Data Breaches scheme", "ISO 27001", "HIPAA (if US-facing)"],
        "system_impact": "High",
        "control_priority": "high",
    },
    "finance": {
        "keywords": ["finance", "financial", "bank", "payment", "credit card", "transaction", "investment", "trading", "fintech", "insurance", "lending", "mortgage"],
        "extra_data_types": ["Payment Card Data (PCI)", "Financial Records", "KYC/AML Data"],
        "extra_assets": ["Credit card numbers", "Bank account details", "Transaction history", "Financial reports"],
        "extra_threats": [
            {"name": "Payment Card Data Theft", "category": "I", "threat_source": "external_hacker", "likelihood": 4, "impact": 5, "default_vulns": ["Unencrypted card data", "Memory scraping possible", "Insufficient network segmentation"], "compliance": ["PCI DSS 4.0", "APRA CPS 234"]},
            {"name": "Transaction Fraud", "category": "T", "threat_source": "external_hacker", "likelihood": 4, "impact": 4, "default_vulns": ["Weak transaction verification", "Missing anomaly detection"], "compliance": ["AML/CTF Act", "AUSTRAC"]},
            {"name": "Insider Trading Data Leak", "category": "I", "threat_source": "insider_malicious", "likelihood": 2, "impact": 5, "default_vulns": ["No data classification", "Unmonitored data export"], "compliance": ["ASIC", "Corporations Act"]},
        ],
        "regulatory_frameworks": ["PCI DSS 4.0", "APRA CPS 234", "AML/CTF Act", "ISO 27001"],
        "system_impact": "High",
        "control_priority": "high",
    },
    "saas": {
        "keywords": ["saas", "software as a service", "multi-tenant", "subscription", "cloud service", "platform"],
        "extra_data_types": ["Customer Data", "Tenant Data", "Subscription/Payment Info"],
        "extra_assets": ["Tenant isolation config", "Customer PII", "Billing data", "Usage analytics"],
        "extra_threats": [
            {"name": "Multi-Tenant Data Leak — Cross-tenant access", "category": "I", "threat_source": "external_hacker", "likelihood": 3, "impact": 4, "default_vulns": ["Broken tenant isolation", "Shared cache contamination", "IDOR on tenant resources"]},
            {"name": "Supply Chain Attack — Third-party dependency", "category": "T", "threat_source": "supply_chain", "likelihood": 3, "impact": 5, "default_vulns": ["Unpinned dependencies", "No SBOM", "Unsigned container images"]},
            {"name": "API Key Leak — Customer credential exposure", "category": "I", "threat_source": "external_hacker", "likelihood": 3, "impact": 4, "default_vulns": ["API keys in logs", "No key rotation", "Insufficient rate limiting"]},
        ],
        "regulatory_frameworks": ["SOC 2 Type II", "ISO 27001", "GDPR (if EU)", "Australian Privacy Act"],
        "system_impact": "Moderate",
        "control_priority": "medium",
    },
    "ecommerce": {
        "keywords": ["ecommerce", "e-commerce", "online store", "shop", "retail", "marketplace", "cart", "checkout"],
        "extra_data_types": ["Payment Data", "Order History", "Customer PII"],
        "extra_assets": ["Product catalog", "Order data", "Customer accounts", "Payment details"],
        "extra_threats": [
            {"name": "Payment Card Skimming (Magecart)", "category": "T", "threat_source": "external_hacker", "likelihood": 4, "impact": 4, "default_vulns": ["No CSP", "Third-party script injection", "Client-side validation only"]},
            {"name": "Account Credential Stuffing", "category": "S", "threat_source": "external_hacker", "likelihood": 4, "impact": 3, "default_vulns": ["No MFA", "No rate limiting at login", "Password reuse allowed"]},
            {"name": "Inventory/Price Manipulation", "category": "T", "threat_source": "external_hacker", "likelihood": 2, "impact": 3, "default_vulns": ["Client-side trust", "No server-side validation"]},
        ],
        "regulatory_frameworks": ["PCI DSS 4.0", "Australian Consumer Law", "Privacy Act"],
        "system_impact": "Moderate",
        "control_priority": "medium",
    },
}

# ── Generic threat templates by system type ──────────────────────────
GENERIC_THREATS: dict[str, list[dict]] = {
    "public_web": [
        {"name": "SQL Injection", "category": "T", "threat_source": "external_hacker", "likelihood": 4, "impact": 5, "default_vulns": ["Input validation gaps", "Dynamic SQL queries", "ORM bypass possible"]},
        {"name": "Cross-Site Scripting (XSS)", "category": "T", "threat_source": "external_hacker", "likelihood": 4, "impact": 4, "default_vulns": ["Unsanitized output", "No CSP headers", "Rich text input allowed"]},
        {"name": "Credential Stuffing / Brute Force", "category": "S", "threat_source": "external_hacker", "likelihood": 4, "impact": 4, "default_vulns": ["No rate limiting", "No MFA", "Weak password policy"]},
        {"name": "Session Hijacking", "category": "S", "threat_source": "external_hacker", "likelihood": 3, "impact": 4, "default_vulns": ["Predictable session IDs", "No HttpOnly cookies", "Session fixation possible"]},
        {"name": "Denial of Service (Layer 7)", "category": "D", "threat_source": "hacktivist", "likelihood": 3, "impact": 3, "default_vulns": ["No rate limiting", "Expensive queries possible", "No CDN"]},
        {"name": "Broken Access Control (IDOR)", "category": "E", "threat_source": "external_hacker", "likelihood": 3, "impact": 4, "default_vulns": ["Direct object references", "Missing authorization checks", "Role confusion"]},
        {"name": "Information Disclosure via Errors", "category": "I", "threat_source": "external_hacker", "likelihood": 3, "impact": 3, "default_vulns": ["Detailed error messages", "Stack traces exposed", "Debug mode enabled"]},
        {"name": "Man-in-the-Middle (Downgrade)", "category": "I", "threat_source": "external_hacker", "likelihood": 2, "impact": 4, "default_vulns": ["No HSTS", "Mixed content", "Weak cipher suites"]},
        {"name": "Log Tampering / Deletion", "category": "R", "threat_source": "insider_malicious", "likelihood": 2, "impact": 3, "default_vulns": ["Logs on same server", "No integrity checks", "Insufficient log retention"]},
        {"name": "Supply Chain — Dependency Compromise", "category": "T", "threat_source": "supply_chain", "likelihood": 3, "impact": 5, "default_vulns": ["Unpinned dependencies", "No SBOM", "Auto-update enabled"]},
    ],
    "api_exposed": [
        {"name": "API Abuse / Rate Limit Bypass", "category": "D", "threat_source": "external_hacker", "likelihood": 4, "impact": 3, "default_vulns": ["Insufficient rate limiting", "No API gateway"]},
        {"name": "Broken Object-Level Authorization", "category": "E", "threat_source": "external_hacker", "likelihood": 4, "impact": 4, "default_vulns": ["IDOR on resource endpoints", "Missing ownership checks"]},
        {"name": "Mass Assignment", "category": "T", "threat_source": "external_hacker", "likelihood": 3, "impact": 4, "default_vulns": ["No input filtering", "Object binding without allowlist"]},
        {"name": "JWT Token Manipulation", "category": "S", "threat_source": "external_hacker", "likelihood": 3, "impact": 4, "default_vulns": ["None algorithm accepted", "Weak signing key", "No expiry validation"]},
    ],
    "mobile": [
        {"name": "Insecure Local Storage", "category": "I", "threat_source": "external_hacker", "likelihood": 3, "impact": 4, "default_vulns": ["Credentials in UserDefaults", "No keychain/keystore", "Cache not encrypted"]},
        {"name": "Certificate Pinning Bypass", "category": "S", "threat_source": "external_hacker", "likelihood": 2, "impact": 4, "default_vulns": ["No cert pinning", "Trusts user CAs"]},
    ],
    "infrastructure": [
        {"name": "SSH Brute Force", "category": "S", "threat_source": "external_hacker", "likelihood": 4, "impact": 4, "default_vulns": ["Password auth enabled", "No fail2ban", "Port 22 exposed"]},
        {"name": "Privilege Escalation — Server", "category": "E", "threat_source": "insider_malicious", "likelihood": 2, "impact": 5, "default_vulns": ["Sudo misconfiguration", "Kernel exploits", "Container breakout"]},
        {"name": "Cloud Metadata SSRF", "category": "I", "threat_source": "external_hacker", "likelihood": 2, "impact": 5, "default_vulns": ["IMDSv1 enabled", "No network boundary for metadata"]},
        {"name": "Unpatched Vulnerability Exploit", "category": "T", "threat_source": "external_hacker", "likelihood": 3, "impact": 4, "default_vulns": ["No auto-patching", "Delayed patching cycle"]},
    ],
    "database": [
        {"name": "Unauthorized Direct Access", "category": "I", "threat_source": "insider_malicious", "likelihood": 3, "impact": 5, "default_vulns": ["No network isolation", "Default credentials", "Exposed port"]},
        {"name": "SQL Injection → Database", "category": "T", "threat_source": "external_hacker", "likelihood": 3, "impact": 5, "default_vulns": ["Application SQL injection", "No prepared statements"]},
        {"name": "Backup Data Theft", "category": "I", "threat_source": "external_hacker", "likelihood": 2, "impact": 5, "default_vulns": ["Unencrypted backups", "Backup stored alongside production"]},
    ],
    "desktop": [
        {"name": "Local Configuration Tampering", "category": "T", "threat_source": "external_hacker", "likelihood": 2, "impact": 3, "default_vulns": ["Config files writable", "No code signing"]},
        {"name": "Memory Credential Extraction", "category": "I", "threat_source": "external_hacker", "likelihood": 2, "impact": 4, "default_vulns": ["Tokens in plaintext memory", "No secure enclave"]},
    ],
    "iot": [
        {"name": "Firmware Extraction / Reverse", "category": "I", "threat_source": "external_hacker", "likelihood": 3, "impact": 3, "default_vulns": ["No secure boot", "Firmware unencrypted", "Debug interfaces enabled"]},
        {"name": "Device Spoofing", "category": "S", "threat_source": "external_hacker", "likelihood": 3, "impact": 4, "default_vulns": ["No device authentication", "Shared default credentials"]},
    ],
}

# ── Control baselines by system impact (FIPS 199) ───────────────────
CONTROL_BASELINES = {
    "Low": ["AC-2", "AU-2", "IA-2", "SC-7", "SI-2"],
    "Moderate": ["AC-2", "AC-3", "AC-6", "AU-2", "AU-6", "IA-2", "IA-5",
                 "SC-7", "SC-8", "SI-2", "SI-3", "RA-5"],
    "High": ["AC-2", "AC-3", "AC-6", "AC-17", "AU-2", "AU-6", "AU-12",
             "IA-2", "IA-5", "SC-7", "SC-8", "SC-12", "SI-2", "SI-3",
             "IR-4", "CP-2", "CP-10", "RA-5", "CA-7"],
}
