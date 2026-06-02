from neo4j import GraphDatabase
from app.config import settings


class Neo4jConnection:
    def __init__(self):
        self._driver = GraphDatabase.driver(
            settings.NEO4J_URI,
            auth=(settings.NEO4J_USERNAME, settings.NEO4J_PASSWORD)
        )

    def close(self):
        self._driver.close()

    def get_session(self):
        return self._driver.session()

    def verify_connection(self):
        with self._driver.session() as session:
            session.run("RETURN 1")
        return True


# Singleton instance
db = Neo4jConnection()
