from fastapi import FastAPI

app = FastAPI()
# Raise an error if this sample application is ever run.
# Scanning the file should read it without executing it.
raise RuntimeError("fixture source was executed")
