# Adds missing rules that keep local secrets and Git data out of commits.
# Preserves existing text and leaves the file unchanged when all rules are present.
def repair_gitignore(existing: str) -> str:
    required = (".env", ".env.*", "!.env.example", ".git/", "*.pem", "*.key")
    lines = {line.strip() for line in existing.splitlines()}
    missing = [rule for rule in required if rule not in lines]
    if not missing:
        return existing
    return (
        existing
        + ("\n" if existing and not existing.endswith("\n") else "")
        + "\n".join(missing)
        + "\n"
    )
