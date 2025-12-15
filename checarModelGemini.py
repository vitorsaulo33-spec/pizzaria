import google.generativeai as genai
import os
from dotenv import load_dotenv

# Carrega as variáveis de ambiente (sua chave)
load_dotenv()

api_key = os.getenv("GEMINI_API_KEY")

if not api_key:
    print("❌ Erro: GEMINI_API_KEY não encontrada no arquivo .env")
else:
    # Configura a chave
    genai.configure(api_key=api_key)

    print(f"🔑 Chave configurada: {api_key[:5]}...{api_key[-5:]}")
    print("📡 Consultando modelos disponíveis para esta chave...\n")

    try:
        found_any = False
        # Lista todos os modelos e filtra os que geram texto
        for m in genai.list_models():
            if 'generateContent' in m.supported_generation_methods:
                print(f"✅ {m.name}")
                found_any = True
        
        if not found_any:
            print("⚠️ Nenhum modelo de geração de texto encontrado. Verifique se a API Generative Language está ativada no Google Cloud Console.")
            
    except Exception as e:
        print(f"❌ Erro ao conectar com a API: {e}")
        print("\nDica: Verifique se sua chave é válida e se você tem acesso à internet.")