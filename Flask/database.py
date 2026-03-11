import psycopg2
import os
import time
from dotenv import load_dotenv

load_dotenv()
def connect_to_database():
    retry_delay = 5
    max_retries = 12
    tries = 0
    while tries < max_retries:
        try:
            connection = psycopg2.connect(
                database=os.getenv('DB_NAME'),
                user=os.getenv('DB_USER'),
                password=os.getenv('DB_PASSWORD'),
                host=os.getenv('DB_HOST'),
                port=os.getenv('DB_PORT')
                )
            cursor = connection.cursor()
            #print("Successfully connected to the database!")
            return cursor, connection
        
        except psycopg2.OperationalError as e:
            tries += 1
            error_msg = f"Database connection failed: {e}"
            print("\033[93mError:\033[0m", error_msg)
            if tries >= max_retries:
                error_msg = "Max retries reached. Exiting..."
                print("\033[93mError:\033[0m", error_msg)
                return None, None
            else:
                warning_msg = f"Retrying in {retry_delay} seconds..."
                print("\033[93mWarning:\033[0m", warning_msg)
                time.sleep(retry_delay)

def close_db_connection(cursor, connection):
    #print("Closing cursor and connection...")
    cursor.close()
    connection.close()

if __name__ == "__main__":
    os.environ['DB_HOST'] = 'localhost'
    os.environ['DB_PORT'] = '5432'
    # Connect to the database
    cursor, connection = connect_to_database()
    # Close the database connection
    close_db_connection(cursor, connection)