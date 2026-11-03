from fastapi import FastAPI

app = FastAPI()
# Scanners must never execute even apparently harmless application modules.
raise RuntimeError("fixture source was executed")
