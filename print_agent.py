import win32print
import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware
import os

app = FastAPI(title="ALIV Print Agent")

# Permite que o site (nuvem) converse com esse script local
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

class PrintJob(BaseModel):
    printer_name: str = "ELGIN i8" # Nome da impressora no Windows
    content: str
    cut_paper: bool = True

def raw_print(printer_name, text_content):
    """Envia texto puro para a impressora (RAW)"""
    try:
        # Se o nome for genérico, tenta pegar a padrão do Windows
        if printer_name == "Padrao":
            printer_name = win32print.GetDefaultPrinter()
            
        hPrinter = win32print.OpenPrinter(printer_name)
        try:
            hJob = win32print.StartDocPrinter(hPrinter, 1, ("ALIV Pedido", None, "RAW"))
            try:
                win32print.StartPagePrinter(hPrinter)
                
                # Comandos ESC/POS (Padrão Epson/Bematech/Elgin)
                # Init + Texto + Pula Linhas
                data = b'\x1B\x40' # Init
                data += text_content.encode('cp850', errors='ignore') # Codificação Brasil
                data += b'\n\n\n\n' # Margem final
                
                # Comando de Corte (Guilhotina)
                data += b'\x1D\x56\x42\x00' 
                
                win32print.WritePrinter(hPrinter, data)
                win32print.EndPagePrinter(hPrinter)
            finally:
                win32print.EndDocPrinter(hPrinter)
        finally:
            win32print.ClosePrinter(hPrinter)
        return True
    except Exception as e:
        print(f"Erro de impressão: {e}")
        return False

@app.post("/print")
def print_endpoint(job: PrintJob):
    print(f"🖨️ Imprimindo pedido na impressora: {job.printer_name}")
    success = raw_print(job.printer_name, job.content)
    if not success:
        raise HTTPException(status_code=500, detail="Falha na impressora local")
    return {"status": "ok"}

if __name__ == "__main__":
    # Roda na porta 5000 do computador local
    print("🚀 ALIV Agent rodando! Minimize esta janela e abra o sistema.")
    uvicorn.run(app, host="0.0.0.0", port=5000)