import psycopg2
import os
from dotenv import load_dotenv

# Go to Flask directory to load .env
dotenv_path = r"c:\Users\henri\Desktop\DIVS Projects\DOA Mic Tower\Software\Gateway\http_endpoint\.env"
if os.path.exists(dotenv_path):
    load_dotenv(dotenv_path)
else:
    load_dotenv()

try:
    connection = psycopg2.connect(
        database=os.getenv('POSTGRES_DB', 'postgres'),
        user=os.getenv('POSTGRES_USER', 'postgres'),
        password=os.getenv('POSTGRES_PASSWORD', 'postgres'),
        host='localhost',
        port=os.getenv('POSTGRES_PORT', '5432')
    )
    cursor = connection.cursor()
    
    cursor.execute("SELECT * FROM nodes")
    nodes = cursor.fetchall()
    print("--- NODES ---")
    for n in nodes:
        print(n)
        
    cursor.execute("SELECT * FROM gateways")
    gateways = cursor.fetchall()
    print("--- GATEWAYS ---")
    for g in gateways:
        print(g)
        
    cursor.close()
    connection.close()
except Exception as e:
    print("Error:", e)

