from typing import Dict, List, Union, Any, Optional, Tuple
import pandas as pd
import plotly.graph_objects as go
import datetime
import sqlite3
from qdrant_client import models
from qdrant_client.http.models import Distance, VectorParams
import os
import base64

from talk2sql.vector_store.qdrant import QdrantVectorStore
from talk2sql.llm.azure_openai import AzureOpenAILLM

class Talk2SQLAzure(QdrantVectorStore, AzureOpenAILLM):
    """
    Main Talk2SQL engine that combines Qdrant vector storage and Azure OpenAI capabilities.
    This class replaces the original Anthropic implementation with Azure OpenAI services.
    """
    
    def __init__(self, config=None):
        """
        Initialize Talk2SQL with Azure OpenAI and Qdrant.
        
        Args:
            config: Configuration dictionary with options for both QdrantVectorStore and AzureOpenAILLM
              - max_retry_attempts: Maximum number of retries for failed SQL queries (default: 3)
              - save_query_history: Whether to save query history with errors and retries (default: True)
              - azure_api_key: Azure OpenAI API key (or use AZURE_OPENAI_API_KEY env var)
              - azure_endpoint: Azure OpenAI endpoint (or use AZURE_ENDPOINT env var)
              - azure_api_version: Azure API version (or use AZURE_API_VERSION env var)
              - azure_deployment: GPT deployment name (or use AZURE_DEPLOYMENT env var)
              - azure_embedding_deployment: Embedding model name (default: "text-embedding-ada-002")
              - history_db_path: Path to SQLite database for storing query history (default: "query_history.db")
        """
        config = config or {}
        
        # Initialize QdrantVectorStore first
        QdrantVectorStore.__init__(self, config)
        
        # Save reference to Qdrant client before it gets overwritten
        self.qdrant_client = self.client
        
        # Now initialize AzureOpenAILLM (which will overwrite self.client)
        AzureOpenAILLM.__init__(self, config)
        
        # Now that both clients are initialized, set up the collections
        self._setup_collections()
        
        # Additional configuration
        self.debug_mode = config.get("debug_mode", False)
        self.auto_visualization = config.get("auto_visualization", True)
        self.max_retry_attempts = config.get("max_retry_attempts", 3)
        self.save_query_history = config.get("save_query_history", True)
        self.history_db_path = config.get("history_db_path", "query_history.db")
        
        # Initialize query history DB
        self._init_history_db()
        
        # SQLite connection for querying databases
        self.conn = None
    
    def connect_to_sqlite(self, db_path: str):
        """
        Connect to a SQLite database.
        
        Args:
            db_path: Path to the SQLite database file
        """
        try:
            # Verify database file exists
            if not os.path.exists(db_path):
                raise FileNotFoundError(f"Database file not found: {db_path}")
            
            # Get file size to verify it's not empty
            file_size = os.path.getsize(db_path)
            if file_size == 0:
                raise ValueError(f"Database file is empty (0 bytes): {db_path}")
            
            if self.debug_mode:
                print(f"Found database file: {db_path} ({file_size} bytes)")
            
            # Connect to database
            self.conn = sqlite3.connect(db_path)
            
            # Test connection with a simple query
            test_cursor = self.conn.cursor()
            test_cursor.execute("PRAGMA database_list")
            db_info = test_cursor.fetchall()
            if self.debug_mode:
                print(f"Database info: {db_info}")
            
            # Define the run_sql function to use this connection
            def run_sql(sql_query):
                return pd.read_sql_query(sql_query, self.conn)
            
            # Set the run_sql function
            self.run_sql = run_sql
            self.run_sql_is_set = True
            
            # Check for tables
            tables_cursor = self.conn.cursor()
            tables_cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
            tables = tables_cursor.fetchall()
            
            if self.debug_mode:
                if tables:
                    table_names = [t[0] for t in tables]
                    print(f"Connected to SQLite database: {db_path}")
                    print(f"Available tables: {', '.join(table_names)}")
                else:
                    print(f"Connected to SQLite database: {db_path}, but no tables found")
                
            return True
        except Exception as e:
            if self.debug_mode:
                print(f"Error connecting to SQLite database: {e}")
                import traceback
                traceback.print_exc()
            raise e
    
    # Override vector store methods to ensure they work correctly
    def add_question_sql(self, question: str, sql: str) -> str:
        """
        Add question-SQL pair to vector store.
        
        Args:
            question: Natural language question
            sql: Corresponding SQL query
            
        Returns:
            ID of the stored entry
        """
        # Create a composite representation
        content = f"Question: {question}\nSQL: {sql}"
        
        # Generate deterministic ID for deduplication
        point_id = self._generate_deterministic_id(content)
        
        # Get embedding
        embedding = self.generate_embedding(question)
        
        # Insert into questions collection
        self.qdrant_client.upsert(
            collection_name=self.questions_collection,
            points=[
                models.PointStruct(
                    id=point_id,
                    vector=embedding,
                    payload={
                        "question": question,
                        "sql": sql
                    }
                )
            ]
        )
        
        return f"{point_id}-q"
    
    def add_schema(self, schema: str) -> str:
        """
        Add database schema to vector store.
        
        Args:
            schema: Database schema (DDL)
            
        Returns:
            ID of the stored entry
        """
        # Generate deterministic ID for deduplication
        point_id = self._generate_deterministic_id(schema)
        
        # Get embedding
        embedding = self.generate_embedding(schema)
        
        # Insert into schema collection
        self.qdrant_client.upsert(
            collection_name=self.schema_collection,
            points=[
                models.PointStruct(
                    id=point_id,
                    vector=embedding,
                    payload={
                        "schema": schema
                    }
                )
            ]
        )
        
        return f"{point_id}-s"
    
    def add_documentation(self, documentation: str) -> str:
        """
        Add documentation to vector store.
        
        Args:
            documentation: Documentation text
            
        Returns:
            ID of the stored entry
        """
        # Generate deterministic ID for deduplication
        point_id = self._generate_deterministic_id(documentation)
        
        # Get embedding
        embedding = self.generate_embedding(documentation)
        
        # Insert into documentation collection
        self.qdrant_client.upsert(
            collection_name=self.docs_collection,
            points=[
                models.PointStruct(
                    id=point_id,
                    vector=embedding,
                    payload={
                        "documentation": documentation
                    }
                )
            ]
        )
        
        return f"{point_id}-d"
    
    def _init_history_db(self):
        """
        Initialize the SQLite database for query history.
        """
        if not self.save_query_history:
            return
            
        try:
            import sqlite3
            
            # Connect to the history database
            history_conn = sqlite3.connect(self.history_db_path)
            cursor = history_conn.cursor()
            
            # Create table if it doesn't exist
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS query_history (
                    id TEXT PRIMARY KEY,
                    timestamp TEXT,
                    question TEXT,
                    sql TEXT,
                    success INTEGER,
                    error_message TEXT,
                    retry_count INTEGER,
                    data BLOB,
                    columns TEXT,
                    visualization BLOB,
                    summary TEXT,
                    total_time_ms REAL,
                    sql_generation_time_ms REAL,
                    sql_execution_time_ms REAL,
                    visualization_time_ms REAL,
                    explanation_time_ms REAL,
                    timing_details TEXT,
                    used_memory INTEGER
                )
            ''')
            
            # Check if the used_memory column exists, add it if it doesn't
            try:
                cursor.execute("SELECT used_memory FROM query_history LIMIT 1")
            except sqlite3.OperationalError:
                # Column doesn't exist, add it
                cursor.execute("ALTER TABLE query_history ADD COLUMN used_memory INTEGER DEFAULT 0")
                if self.debug_mode:
                    print("Added used_memory column to query_history table")
            
            history_conn.commit()
            history_conn.close()
            
            if self.debug_mode:
                print(f"Initialized query history database at {self.history_db_path}")
                
        except Exception as e:
            if self.debug_mode:
                print(f"Error initializing query history database: {e}")
                
            # Fallback to in-memory storage
            self.query_history = []
            self.save_query_history = False
    
    def record_query_attempt(self, question: str, sql: str, success: bool, error_message: str = None, 
                           retry_count: int = 0, data: pd.DataFrame = None, columns: List[str] = None,
                           visualization = None, summary: str = None, total_time_ms: float = None,
                           sql_generation_time_ms: float = None, sql_execution_time_ms: float = None,
                           visualization_time_ms: float = None, explanation_time_ms: float = None,
                           timing_details: Dict = None, used_memory: bool = None):
        """
        Record a query attempt in the history.
        
        Args:
            question: The natural language question
            sql: The SQL query
            success: Whether the query succeeded
            error_message: Error message if the query failed
            retry_count: Number of retries performed
            data: DataFrame result of the query (for successful queries)
            columns: DataFrame columns (for successful queries)
            visualization: Plotly visualization (for successful queries)
            summary: Natural language summary of the results (for successful queries)
            total_time_ms: Total query time in milliseconds
            sql_generation_time_ms: Time to generate SQL in milliseconds
            sql_execution_time_ms: Time to execute SQL in milliseconds
            visualization_time_ms: Time to generate visualization in milliseconds
            explanation_time_ms: Time to generate explanation in milliseconds
            timing_details: Detailed timing information as a dictionary
            used_memory: Whether memory/context was used in generating the SQL
        """
        if not self.save_query_history:
            return
            
        try:
            import sqlite3
            import pickle
            import json
            
            # Generate a unique ID
            entry_id = str(hash(f"{question}_{datetime.datetime.now().isoformat()}"))
            timestamp = datetime.datetime.now().isoformat()
            
            # Use the class attribute if used_memory parameter is not provided
            if used_memory is None:
                used_memory = getattr(self, 'last_query_used_memory', False)
                
            # Connect to the history database
            history_conn = sqlite3.connect(self.history_db_path)
            cursor = history_conn.cursor()
            
            # Convert DataFrame to JSON string if it exists (instead of pickle)
            data_json = None
            if success and data is not None:
                try:
                    if hasattr(data, 'to_json'):
                        # Convert DataFrame to JSON string
                        data_json = data.to_json(orient='records')
                    else:
                        # If not a DataFrame, try basic JSON serialization
                        data_json = json.dumps(str(data))
                except Exception as e:
                    if self.debug_mode:
                        print(f"Error converting DataFrame to JSON: {e}")
                    # Fallback to string representation
                    data_json = json.dumps(str(data))
                
            # Store columns as JSON
            columns_json = None
            if columns:
                columns_json = json.dumps(columns)
                
            # Continue using pickle for visualization (it's complex to convert)
            vis_blob = None
            if success and visualization is not None:
                vis_blob = pickle.dumps(visualization)
                
            # Convert timing details to JSON if it exists
            timing_details_json = None
            if timing_details:
                timing_details_json = json.dumps(timing_details)
                
            # Insert the record
            try:
                cursor.execute(
                    '''
                    INSERT INTO query_history 
                    (id, timestamp, question, sql, success, error_message, retry_count, data, columns, 
                    visualization, summary, total_time_ms, sql_generation_time_ms, sql_execution_time_ms,
                    visualization_time_ms, explanation_time_ms, timing_details, used_memory) 
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''',
                    (entry_id, timestamp, question, sql, 1 if success else 0, error_message, retry_count,
                     data_json, columns_json, vis_blob, summary, total_time_ms, sql_generation_time_ms,
                     sql_execution_time_ms, visualization_time_ms, explanation_time_ms, timing_details_json,
                     1 if used_memory else 0)
                )
            except sqlite3.OperationalError as e:
                # If the column doesn't exist yet (older DB), try without used_memory
                if "used_memory" in str(e):
                    cursor.execute(
                        '''
                        INSERT INTO query_history 
                        (id, timestamp, question, sql, success, error_message, retry_count, data, columns, 
                        visualization, summary, total_time_ms, sql_generation_time_ms, sql_execution_time_ms,
                        visualization_time_ms, explanation_time_ms, timing_details) 
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ''',
                        (entry_id, timestamp, question, sql, 1 if success else 0, error_message, retry_count,
                         data_json, columns_json, vis_blob, summary, total_time_ms, sql_generation_time_ms,
                         sql_execution_time_ms, visualization_time_ms, explanation_time_ms, timing_details_json)
                    )
                else:
                    raise e
            
            history_conn.commit()
            history_conn.close()
            
        except Exception as e:
            if self.debug_mode:
                print(f"Error recording query history to database: {e}")
                import traceback
                traceback.print_exc()
    
    def get_query_history(self, successful_only: bool = False, with_errors_only: bool = False, limit: int = None):
        """
        Get the query history.
        
        Args:
            successful_only: Only return successful queries
            with_errors_only: Only return queries that had errors
            limit: Maximum number of queries to return
            
        Returns:
            List of query history entries
        """
        if not self.save_query_history:
            return []
            
        try:
            import sqlite3
            import pickle
            import json
            
            # Connect to the history database
            history_conn = sqlite3.connect(self.history_db_path)
            cursor = history_conn.cursor()
            
            # Build the query
            query = "SELECT * FROM query_history"
            conditions = []
            
            if successful_only:
                conditions.append("success = 1")
                
            if with_errors_only:
                conditions.append("error_message IS NOT NULL")
                
            if conditions:
                query += " WHERE " + " AND ".join(conditions)
                
            query += " ORDER BY timestamp DESC"
            
            if limit:
                query += f" LIMIT {limit}"
                
            # Execute the query
            cursor.execute(query)
            rows = cursor.fetchall()
            
            # Get column names
            column_names = [description[0] for description in cursor.description]
            
            # Convert rows to dictionaries
            result = []
            for row in rows:
                # Create a dict with all columns
                entry = dict(zip(column_names, row))
                
                # Convert success to boolean
                entry["success"] = bool(entry.get("success", 0))
                
                # Convert used_memory to boolean if it exists
                if "used_memory" in entry:
                    entry["used_memory"] = bool(entry.get("used_memory", 0))
                else:
                    entry["used_memory"] = False  # Default for older entries
                
                # Deserialize data if it exists - now expects JSON string
                if entry.get("data"):
                    try:
                        # First try to parse as JSON (for newer entries)
                        if isinstance(entry["data"], str):
                            try:
                                # Try to parse as JSON array of records
                                data_list = json.loads(entry["data"])
                                # Convert back to DataFrame if it's a list of records
                                if isinstance(data_list, list):
                                    entry["data"] = pd.DataFrame(data_list)
                                else:
                                    entry["data"] = data_list
                            except json.JSONDecodeError:
                                # If not valid JSON, keep as string
                                entry["data"] = entry["data"]
                        # For backward compatibility - try pickle for older entries
                        elif isinstance(entry["data"], bytes):
                            try:
                                entry["data"] = pickle.loads(entry["data"])
                            except Exception as e:
                                # If pickle fails, convert bytes to base64 string
                                try:
                                    import base64
                                    entry["data"] = base64.b64encode(entry["data"]).decode('utf-8')
                                except:
                                    entry["data"] = str(entry["data"])
                    except Exception as e:
                        if self.debug_mode:
                            print(f"Error deserializing data: {e}")
                        entry["data"] = str(entry["data"])
                
                # Deserialize columns if they exist
                if entry.get("columns"):
                    try:
                        if isinstance(entry["columns"], bytes):
                            entry["columns"] = json.loads(entry["columns"].decode('utf-8'))
                        else:
                            entry["columns"] = json.loads(entry["columns"])
                    except:
                        entry["columns"] = None
                
                # Deserialize visualization if it exists
                if entry.get("visualization"):
                    try:
                        entry["visualization"] = pickle.loads(entry["visualization"])
                    except Exception as e:
                        # If pickle fails, handle bytes by converting to base64 string
                        if isinstance(entry["visualization"], bytes):
                            try:
                                import base64
                                entry["visualization"] = base64.b64encode(entry["visualization"]).decode('utf-8')
                            except:
                                entry["visualization"] = None
                        else:
                            entry["visualization"] = None
                
                # Deserialize timing details if they exist
                if entry.get("timing_details"):
                    try:
                        if isinstance(entry["timing_details"], bytes):
                            entry["timing_details"] = json.loads(entry["timing_details"].decode('utf-8'))
                        else:
                            entry["timing_details"] = json.loads(entry["timing_details"])
                    except:
                        entry["timing_details"] = None
                
                result.append(entry)
            
            history_conn.close()
            return result
            
        except Exception as e:
            if self.debug_mode:
                print(f"Error retrieving query history from database: {e}")
                import traceback
                traceback.print_exc()
            return []
    
    def analyze_error_patterns(self):
        """
        Analyze error patterns in query history.
        
        Returns:
            Dictionary with error analysis
        """
        history = self.get_query_history()
        
        if not history:
            return {"message": "No query history available for analysis"}
        
        # Count total queries and errors
        total_queries = len(history)
        error_queries = len([q for q in history if not q["success"]])
        retried_queries = len([q for q in history if q["retry_count"] > 0])
        successful_retries = len([q for q in history if q["retry_count"] > 0 and q["success"]])
        
        # Group errors by type
        error_types = {}
        for query in history:
            if query["error_message"]:
                # Extract error type (first line or up to first colon)
                error_type = query["error_message"].split('\n')[0]
                if ':' in error_type:
                    error_type = error_type.split(':', 1)[0]
                
                if error_type in error_types:
                    error_types[error_type] += 1
                else:
                    error_types[error_type] = 1
        
        # Calculate retry effectiveness
        retry_success_rate = (successful_retries / retried_queries) if retried_queries > 0 else 0
        
        return {
            "total_queries": total_queries,
            "error_queries": error_queries,
            "error_rate": error_queries / total_queries if total_queries > 0 else 0,
            "retried_queries": retried_queries,
            "successful_retries": successful_retries,
            "retry_success_rate": retry_success_rate,
            "common_error_types": sorted(error_types.items(), key=lambda x: x[1], reverse=True)
        }
    
    def smart_query(self, question: str, print_results: bool = True, visualize: bool = True):
        """
        Execute a query with automatic retry mechanism and detailed reporting.
        
        Args:
            question: Natural language question
            print_results: Whether to print results
            visualize: Whether to generate visualization
            
        Returns:
            Dictionary with query results and metadata
        """
        # Track query metadata and timing
        metadata = {
            "question": question,
            "timestamp": datetime.datetime.now().isoformat(),
            "success": False,
            "retry_count": 0,
            "error_message": None,
            "original_sql": None,
            "final_sql": None,
            "used_memory": False
        }
        
        timing = {
            "start_time": datetime.datetime.now(),
            "sql_generation_start": None,
            "sql_generation_end": None,
            "sql_execution_start": None,
            "sql_execution_end": None,
            "visualization_start": None,
            "visualization_end": None,
            "explanation_start": None,
            "explanation_end": None,
            "end_time": None
        }
        
        # Function to calculate elapsed time in ms
        def elapsed_ms(start, end):
            if start and end:
                return (end - start).total_seconds() * 1000
            return None
        
        # Reset the memory usage tracking
        self.last_query_used_memory = False
        
        # Generate initial SQL
        try:
            timing["sql_generation_start"] = datetime.datetime.now()
            sql_response = self.generate_sql(question)
            sql = self.extract_sql(sql_response)
            timing["sql_generation_end"] = datetime.datetime.now()
            metadata["original_sql"] = sql
            metadata["used_memory"] = getattr(self, 'last_query_used_memory', False)
            
            if print_results:
                print(f"Used memory/context: {metadata['used_memory']}")
        except Exception as e:
            error_message = str(e)
            metadata["error_message"] = error_message
            
            timing["end_time"] = datetime.datetime.now()
            total_time_ms = elapsed_ms(timing["start_time"], timing["end_time"])
            sql_generation_time_ms = elapsed_ms(timing["sql_generation_start"], timing["sql_generation_end"])
            
            # Record the failed attempt with timing data
            self.record_query_attempt(
                question=question, 
                sql=None, 
                success=False, 
                error_message=error_message,
                total_time_ms=total_time_ms,
                sql_generation_time_ms=sql_generation_time_ms,
                timing_details={
                    "total_ms": total_time_ms,
                    "sql_generation_ms": sql_generation_time_ms
                },
                used_memory=metadata["used_memory"]
            )
            
            if print_results:
                print(f"Error generating SQL: {error_message}")
                
            return {
                "success": False,
                "error": error_message,
                "metadata": metadata,
                "timing": {
                    "total_ms": total_time_ms,
                    "sql_generation_ms": sql_generation_time_ms
                },
                "used_memory": metadata["used_memory"]
            }
            
        # Print the initial SQL
        if print_results:
            try:
                from IPython.display import display, Code
                print("Generated SQL:")
                display(Code(sql))
            except ImportError:
                print(f"Generated SQL: {sql}")
                
        # Check if database connection is set
        if not self.run_sql_is_set:
            metadata["error_message"] = "No database connection"
            
            timing["end_time"] = datetime.datetime.now()
            total_time_ms = elapsed_ms(timing["start_time"], timing["end_time"])
            sql_generation_time_ms = elapsed_ms(timing["sql_generation_start"], timing["sql_generation_end"])
            
            self.record_query_attempt(
                question=question, 
                sql=sql, 
                success=False, 
                error_message=metadata["error_message"],
                total_time_ms=total_time_ms,
                sql_generation_time_ms=sql_generation_time_ms,
                timing_details={
                    "total_ms": total_time_ms,
                    "sql_generation_ms": sql_generation_time_ms
                },
                used_memory=metadata["used_memory"]
            )
            
            if print_results:
                print("No database connection. Connect to a database to run queries.")
                
            return {
                "success": False,
                "sql": sql,
                "error": "No database connection",
                "metadata": metadata,
                "timing": {
                    "total_ms": total_time_ms,
                    "sql_generation_ms": sql_generation_time_ms
                },
                "used_memory": metadata["used_memory"]
            }
        
        # Execute SQL with retry mechanism
        current_sql = sql
        retry_count = 0
        df = None
        fig = None
        summary = None
        
        while retry_count <= self.max_retry_attempts:
            try:
                if print_results and retry_count > 0:
                    print(f"\nRetry attempt {retry_count}/{self.max_retry_attempts}:")
                    try:
                        from IPython.display import display, Code
                        display(Code(current_sql))
                    except ImportError:
                        print(f"SQL: {current_sql}")
                
                # Execute the SQL
                timing["sql_execution_start"] = datetime.datetime.now()
                df = self.run_sql(current_sql)
                timing["sql_execution_end"] = datetime.datetime.now()
                
                # Success! Break out of retry loop
                metadata["success"] = True
                metadata["final_sql"] = current_sql
                metadata["retry_count"] = retry_count
                
                # Generate visualization if requested
                if visualize and df is not None and self.should_generate_visualization(df):
                    try:
                        print(f"Attempting to generate visualization for query: '{question}'")
                        print(f"DataFrame shape: {df.shape}")
                        print(f"DataFrame columns: {df.columns.tolist()}")
                        print(f"DataFrame sample:\n{df.head(3)}")
                        
                        print("Generating Plotly code...")
                        timing["visualization_start"] = datetime.datetime.now()
                        plotly_code = self.generate_plotly_code(
                            question=question,
                            sql=current_sql,
                            df_metadata=f"DataFrame info: {df.dtypes}"
                        )
                        print(f"Generated Plotly code:\n{plotly_code}")
                        
                        print("Creating Plotly figure...")
                        fig = self.get_plotly_figure(plotly_code, df)
                        timing["visualization_end"] = datetime.datetime.now()
                        print(f"Figure created: {fig is not None}")
                        
                        if print_results:
                            try:
                                from IPython.display import Image
                                img_bytes = fig.to_image(format="png", scale=2)
                                display(Image(img_bytes))
                            except ImportError:
                                # Prevent opening in a new browser tab by setting auto_open to False
                                # fig.show(config={'displayModeBar': True, 'showLink': False}, auto_open=False)
                                pass
                    except Exception as e:
                        print(f"Visualization error: {e}")
                        import traceback
                        traceback.print_exc()
                else:
                    if not visualize:
                        print("Visualization is disabled")
                    elif df is None:
                        print("DataFrame is None, cannot visualize")
                    else:
                        should_vis = self.should_generate_visualization(df)
                        print(f"should_generate_visualization returned: {should_vis}")
                        print(f"DataFrame details - Shape: {df.shape}, Empty: {df.empty}")
                        if not df.empty:
                            numeric_cols = [col for col in df.columns if df[col].dtype.kind in 'ifc']
                            print(f"Numeric columns: {numeric_cols}")
                
                # Generate summary
                if df is not None and len(df) > 0:
                    try:
                        timing["explanation_start"] = datetime.datetime.now()
                        summary = self.generate_data_summary(question, {"sql": current_sql, "data": df})
                        timing["explanation_end"] = datetime.datetime.now()
                    except Exception as e:
                        print(f"Summary generation error: {e}")
                
                # Calculate timing information
                timing["end_time"] = datetime.datetime.now()
                total_time_ms = elapsed_ms(timing["start_time"], timing["end_time"])
                sql_generation_time_ms = elapsed_ms(timing["sql_generation_start"], timing["sql_generation_end"])
                sql_execution_time_ms = elapsed_ms(timing["sql_execution_start"], timing["sql_execution_end"])
                visualization_time_ms = elapsed_ms(timing["visualization_start"], timing["visualization_end"])
                explanation_time_ms = elapsed_ms(timing["explanation_start"], timing["explanation_end"])
                
                timing_details = {
                    "total_ms": total_time_ms,
                    "sql_generation_ms": sql_generation_time_ms,
                    "sql_execution_ms": sql_execution_time_ms,
                    "visualization_ms": visualization_time_ms,
                    "explanation_ms": explanation_time_ms
                }
                
                # Record the successful attempt with all data
                self.record_query_attempt(
                    question=question,
                    sql=current_sql,
                    success=True,
                    retry_count=retry_count,
                    data=df,
                    columns=list(df.columns) if df is not None else None,
                    visualization=fig,
                    summary=summary,
                    total_time_ms=total_time_ms,
                    sql_generation_time_ms=sql_generation_time_ms,
                    sql_execution_time_ms=sql_execution_time_ms,
                    visualization_time_ms=visualization_time_ms,
                    explanation_time_ms=explanation_time_ms,
                    timing_details=timing_details,
                    used_memory=metadata["used_memory"]
                )
                
                break
                
            except Exception as e:
                error_message = str(e)
                
                # Record time of error
                sql_execution_time_ms = None
                if timing["sql_execution_start"]:
                    error_time = datetime.datetime.now()
                    sql_execution_time_ms = elapsed_ms(timing["sql_execution_start"], error_time)
                
                # Record failed attempt
                self.record_query_attempt(
                    question=question,
                    sql=current_sql,
                    success=False,
                    error_message=error_message,
                    retry_count=retry_count,
                    total_time_ms=elapsed_ms(timing["start_time"], datetime.datetime.now()),
                    sql_generation_time_ms=elapsed_ms(timing["sql_generation_start"], timing["sql_generation_end"]),
                    sql_execution_time_ms=sql_execution_time_ms,
                    timing_details={
                        "total_ms": elapsed_ms(timing["start_time"], datetime.datetime.now()),
                        "sql_generation_ms": elapsed_ms(timing["sql_generation_start"], timing["sql_generation_end"]),
                        "sql_execution_ms": sql_execution_time_ms
                    },
                    used_memory=metadata["used_memory"]
                )
                
                if print_results:
                    print(f"\nSQL Error: {error_message}")
                
                # Check if we've reached max retries
                retry_count += 1
                if retry_count > self.max_retry_attempts:
                    metadata["error_message"] = f"Failed after {self.max_retry_attempts} attempts. Last error: {error_message}"
                    metadata["retry_count"] = retry_count - 1
                    
                    if print_results:
                        print(f"Maximum retry attempts ({self.max_retry_attempts}) exceeded.")
                        
                    break
                
                # Generate corrected SQL
                if print_results:
                    print(f"Attempting to fix query...")
                    
                # Track SQL correction time
                timing["sql_generation_start"] = datetime.datetime.now()
                current_sql = self.generate_sql_with_error_context(
                    question=question,
                    previous_sql=current_sql,
                    error_message=error_message
                )
                timing["sql_generation_end"] = datetime.datetime.now()
        
        # If execution failed after all retries
        if not metadata["success"]:
            timing["end_time"] = datetime.datetime.now()
            total_time_ms = elapsed_ms(timing["start_time"], timing["end_time"])
            
            return {
                "success": False,
                "sql": metadata["original_sql"],
                "corrected_sql": current_sql,
                "error": metadata["error_message"],
                "retry_count": metadata["retry_count"],
                "metadata": metadata,
                "timing": {
                    "total_ms": total_time_ms,
                    "sql_generation_ms": elapsed_ms(timing["sql_generation_start"], timing["sql_generation_end"]),
                    "sql_execution_ms": elapsed_ms(timing["sql_execution_start"], timing["sql_execution_end"]),
                },
                "used_memory": metadata["used_memory"]
            }
        
        # Execution succeeded
        if print_results:
            try:
                from IPython.display import display
                display(df)
            except ImportError:
                print(df)
        
        # Add to training if successful
        if df is not None and len(df) > 0:
            self.add_question_sql(question, metadata["final_sql"])
        
        # Calculate final timing information
        timing["end_time"] = datetime.datetime.now()
        total_time_ms = elapsed_ms(timing["start_time"], timing["end_time"])
        sql_generation_time_ms = elapsed_ms(timing["sql_generation_start"], timing["sql_generation_end"])
        sql_execution_time_ms = elapsed_ms(timing["sql_execution_start"], timing["sql_execution_end"])
        visualization_time_ms = elapsed_ms(timing["visualization_start"], timing["visualization_end"])
        explanation_time_ms = elapsed_ms(timing["explanation_start"], timing["explanation_end"])
        
        timing_details = {
            "total_ms": total_time_ms,
            "sql_generation_ms": sql_generation_time_ms,
            "sql_execution_ms": sql_execution_time_ms,
            "visualization_ms": visualization_time_ms,
            "explanation_ms": explanation_time_ms
        }
        
        # Return successful result with timing information
        return {
            "success": True,
            "sql": metadata["original_sql"],
            "final_sql": metadata["final_sql"] if metadata["final_sql"] != metadata["original_sql"] else None,
            "retry_count": metadata["retry_count"],
            "data": df,
            "visualization": fig,
            "summary": summary,
            "metadata": metadata,
            "timing": timing_details,
            "used_memory": metadata["used_memory"]
        }
    
    def analyze_data(self, 
                    question: str, 
                    visualize: bool = True, 
                    explain: bool = True,
                    suggest_followups: bool = True) -> Dict[str, Any]:
        """
        Comprehensive analysis function with retry mechanism.
        
        Args:
            question: Natural language question
            visualize: Whether to generate visualization
            explain: Whether to generate explanation
            suggest_followups: Whether to suggest follow-up questions
            
        Returns:
            Dictionary with analysis results
        """
        # Use smart_query to handle retries
        result = self.smart_query(question, print_results=False, visualize=visualize)
        
        # If query failed, return error information
        if not result["success"]:
            return result
        
        # Extract results
        df = result["data"]
        sql = result["final_sql"] or result["sql"]
        
        # Add explanation if requested
        if explain and df is not None and len(df) > 0:
            result["explanation"] = self.explain_results(question, df)
        
        # Generate follow-up questions if requested
        if suggest_followups and df is not None:
            df_info = f"Columns: {', '.join(df.columns)}"
            followups = self.generate_follow_up_questions(
                question=question,
                sql=sql,
                result_info=df_info
            )
            result["followup_questions"] = followups
        
        return result
    
    def explain_results(self, question: str, df: pd.DataFrame) -> str:
        """
        Generate explanation of query results.
        
        Args:
            question: Original question
            df: Query results DataFrame
            
        Returns:
            Natural language explanation of results
        """
        # Limit the DataFrame representation for the prompt
        df_str = df.head(10).to_markdown() if len(df) > 10 else df.to_markdown()
        
        prompt = [
            self.system_message(
                f"You are a data analyst explaining query results. "
                f"The user asked: '{question}'\n\n"
                f"Query results:\n{df_str}"
            ),
            self.user_message(
                "Please provide a concise explanation of these results that answers the question. "
                "Highlight any notable patterns, outliers, or insights."
            )
        ]
        
        return self.submit_prompt(prompt)
    
    def demo(self, question: str = None) -> None:
        """
        Run a demonstration of Talk2SQL capabilities with retry mechanism.
        
        Args:
            question: Optional question to start with
        """
        if not question:
            question = input("Enter your question: ")
        
        print(f"\n📝 Question: {question}\n")
        
        # Use smart_query to handle retries
        result = self.smart_query(question, print_results=True)
        
        if result["success"]:
            print("\n✅ Query succeeded!")
            
            if result["retry_count"] > 0:
                print(f"🛠️ Query was fixed after {result['retry_count']} retry attempts")
                
            # Generate explanation
            df = result["data"]
            sql = result["final_sql"] or result["sql"]
            
            explanation = self.explain_results(question, df)
            print(f"\n💡 Explanation:\n{explanation}")
            
            # Follow-up questions
            followups = self.generate_follow_up_questions(
                question=question,
                sql=sql,
                result_info=f"Columns: {', '.join(df.columns)}"
            )
            
            print("\n🔄 Follow-up questions:")
            for i, q in enumerate(followups, 1):
                print(f"{i}. {q}")
        else:
            print(f"\n❌ Query failed after {result['retry_count']} retry attempts")
            print(f"Error: {result['error']}")
            
        print("\n✨ Demo complete!")

    # Override _setup_collections method to use qdrant_client instead of client
    def _setup_collections(self):
        """Create collections if they don't exist."""
        # Questions collection
        if not self.qdrant_client.collection_exists(self.questions_collection):
            self.qdrant_client.create_collection(
                collection_name=self.questions_collection,
                vectors_config=VectorParams(
                    size=self.embedding_size,
                    distance=Distance.COSINE
                )
            )
            
        # Schema collection
        if not self.qdrant_client.collection_exists(self.schema_collection):
            self.qdrant_client.create_collection(
                collection_name=self.schema_collection,
                vectors_config=VectorParams(
                    size=self.embedding_size,
                    distance=Distance.COSINE
                )
            )
            
        # Documentation collection
        if not self.qdrant_client.collection_exists(self.docs_collection):
            self.qdrant_client.create_collection(
                collection_name=self.docs_collection,
                vectors_config=VectorParams(
                    size=self.embedding_size,
                    distance=Distance.COSINE
                )
            )

    def get_similar_questions(self, question: str) -> list:
        """
        Get similar questions with their SQL from vector store.
        
        Args:
            question: Natural language question
            
        Returns:
            List of question-SQL pairs
        """
        embedding = self.generate_embedding(question)
        
        results = self.qdrant_client.search(
            collection_name=self.questions_collection,
            query_vector=embedding,
            limit=self.n_results
        )
        
        return [point.payload for point in results]
    
    def get_related_schema(self, question: str) -> list:
        """
        Get related schema information from vector store.
        
        Args:
            question: Natural language question
            
        Returns:
            List of schema strings
        """
        embedding = self.generate_embedding(question)
        
        results = self.qdrant_client.search(
            collection_name=self.schema_collection,
            query_vector=embedding,
            limit=self.n_results
        )
        
        return [point.payload["schema"] for point in results]
    
    def get_related_documentation(self, question: str) -> list:
        """
        Get related documentation from vector store.
        
        Args:
            question: Natural language question
            
        Returns:
            List of documentation strings
        """
        embedding = self.generate_embedding(question)
        
        results = self.qdrant_client.search(
            collection_name=self.docs_collection,
            query_vector=embedding,
            limit=self.n_results
        )
        
        return [point.payload["documentation"] for point in results]

    def get_all_training_data(self) -> pd.DataFrame:
        """
        Get all training data as a DataFrame.
        
        Returns:
            DataFrame with all training data
        """
        # Initialize empty DataFrame
        df = pd.DataFrame(columns=["id", "type", "question", "content"])
        
        # Get questions
        questions = self.qdrant_client.scroll(
            collection_name=self.questions_collection,
            limit=10000,
            with_payload=True,
            with_vectors=False
        )[0]
        
        for point in questions:
            df = pd.concat([df, pd.DataFrame([{
                "id": f"{point.id}-q",
                "type": "question",
                "question": point.payload["question"],
                "content": point.payload["sql"]
            }])], ignore_index=True)
        
        # Get schema
        schemas = self.qdrant_client.scroll(
            collection_name=self.schema_collection,
            limit=10000,
            with_payload=True,
            with_vectors=False
        )[0]
        
        for point in schemas:
            df = pd.concat([df, pd.DataFrame([{
                "id": f"{point.id}-s",
                "type": "schema",
                "question": None,
                "content": point.payload["schema"]
            }])], ignore_index=True)
        
        # Get documentation
        docs = self.qdrant_client.scroll(
            collection_name=self.docs_collection,
            limit=10000,
            with_payload=True,
            with_vectors=False
        )[0]
        
        for point in docs:
            df = pd.concat([df, pd.DataFrame([{
                "id": f"{point.id}-d",
                "type": "documentation",
                "question": None,
                "content": point.payload["documentation"]
            }])], ignore_index=True)
        
        return df
    
    def remove_training_data(self, id: str) -> bool:
        """
        Remove training data by ID.
        
        Args:
            id: ID of training data to remove
            
        Returns:
            True if successful
        """
        try:
            # Parse ID to get collection
            if id.endswith("-q"):
                collection = self.questions_collection
                real_id = id[:-2]
            elif id.endswith("-s"):
                collection = self.schema_collection
                real_id = id[:-2]
            elif id.endswith("-d"):
                collection = self.docs_collection
                real_id = id[:-2]
            else:
                return False
            
            # Delete from collection
            self.qdrant_client.delete(
                collection_name=collection,
                points_selector=[real_id]
            )
            
            return True
        except Exception as e:
            print(f"Error removing training data: {e}")
            return False
    
    def reset_collection(self, collection_type: str) -> bool:
        """
        Reset a collection to empty state.
        
        Args:
            collection_type: Type of collection ("questions", "schema", "docs")
            
        Returns:
            True if successful
        """
        try:
            if collection_type == "questions":
                self.qdrant_client.delete_collection(self.questions_collection)
            elif collection_type == "schema":
                self.qdrant_client.delete_collection(self.schema_collection)
            elif collection_type == "docs":
                self.qdrant_client.delete_collection(self.docs_collection)
            else:
                return False
            
            self._setup_collections()
            return True
        except Exception as e:
            print(f"Error resetting collection: {e}")
            return False

    def generate_sql(self, question: str) -> str:
        """
        Generate SQL query from natural language question.
        
        Args:
            question: Natural language question
            
        Returns:
            SQL query with configuration metadata
        """
        # Get similar questions
        similar_questions = self.get_similar_questions(question)
        similar_questions_text = ""
        
        # Track if memory/context was used
        used_memory = False
        
        for i, q in enumerate(similar_questions, 1):
            similar_questions_text += f"Example {i}:\nQuestion: {q['question']}\nSQL: {q['sql']}\n\n"
            used_memory = True  # Mark as true if we have similar questions
            
        # Get schema information
        schema_info = self.get_related_schema(question)
        schema_text = "\n".join(schema_info)
        if schema_info:
            used_memory = True  # Mark as true if we have schema info
        
        # Get documentation
        docs = self.get_related_documentation(question)
        docs_text = "\n".join(docs)
        if docs:
            used_memory = True  # Mark as true if we have documentation
        
        # Build prompt
        system_content = (
            "You are an expert SQL developer who translates natural language questions into SQL queries. "
            "You will be provided with example questions and their SQL equivalents, database schema information, "
            "and a question to convert to SQL."
            "IMPORTANT: Generate ONLY valid SQL SELECT queries based on the provided database schema. DO NOT create DELETE, UPDATE, INSERT, or ALTER statements under any circumstances. If the question is unrelated to the database or you're unsure how to answer, generate a safe query like 'SELECT name FROM sqlite_master WHERE type=\"table\"' to show available tables, or 'PRAGMA table_info([table_name])' to show columns. Never respond to non-database questions, personal requests, or generate any text that isn't a valid SQL query. Your response must strictly follow the required format with SQL tags."
            "\n\nYour response should follow this format:"
            "\n<sql>\n[YOUR SQL QUERY HERE]\n</sql>"
            f"\n<config>\n{{ \"used_memory\": {str(used_memory).lower()} }}\n</config>"
            "\n\nThe config section should indicate whether you used memory/context from previous questions."
            "config set to true if you created the query based on the previous questions and their sql, false if you did not use the previous questions and their sql and you created the query based on the schema and documentation alone"
            "The user may ask visuzaliton in their question, example: 'Salary breakdown by team and then by position in a treemap diagram', your job is to generate the sql query that will pull the data best for the creating this visualization from the database"
        )
        
        if schema_text:
            system_content += f"\n\nDatabase Schema:\n{schema_text}"
            
        if docs_text:
            system_content += f"\n\nDatabase Documentation:\n{docs_text}"
            
        if similar_questions_text:
            system_content += f"\n\nSimilar Questions and SQL:\n{similar_questions_text}"
        
        prompt = [
            self.system_message(system_content),
            self.user_message(
                f"Please convert the following question to a valid SQL query:\n\n{question}\n\n"
                f"Return the SQL query with the config section indicating if you used memory."
            )
        ]
        
        # Get response from Azure OpenAI
        response = self.submit_prompt(prompt)
        
        # If the LLM didn't include the config section, add it manually
        if "<config>" not in response:
            sql = self.extract_sql(response)
            config = f"<config>\n{{ \"used_memory\": {str(used_memory).lower()} }}\n</config>"
            return sql + "\n\n" + config
            
        return response
    
    def extract_sql(self, llm_response: str) -> str:
        """Extract SQL query from LLM response text.
        
        Args:
            llm_response: Response text from LLM
            
        Returns:
            SQL query without XML tags
        """
        # Extract the SQL query from between <sql></sql> tags
        import re
        
        # Extract SQL query
        sql_pattern = r"<sql>(.*?)</sql>"
        sql_match = re.search(sql_pattern, llm_response, re.DOTALL)
        
        if sql_match:
            sql = sql_match.group(1).strip()
        else:
            # If no tags, assume the whole response is SQL
            sql = llm_response.strip()
            
            # Remove markdown code block formatting if present
            if sql.startswith("```sql"):
                sql = sql[6:]
            elif sql.startswith("```"):
                sql = sql[3:]
                
            if sql.endswith("```"):
                sql = sql[:-3]
                
        # Also extract config if present
        config_pattern = r"<config>(.*?)</config>"
        config_match = re.search(config_pattern, llm_response, re.DOTALL)
        
        # Parse the config JSON if it exists
        used_memory = False
        if config_match:
            try:
                import json
                config_text = config_match.group(1).strip()
                config = json.loads(config_text)
                used_memory = config.get("used_memory", False)
            except:
                pass
                
        # Store in class attribute for access by other methods
        self.last_query_used_memory = used_memory
            
        return sql.strip()
    
    def generate_sql_with_error_context(self, question: str, previous_sql: str, error_message: str) -> str:
        """
        Generate corrected SQL query based on error message.
        
        Args:
            question: Natural language question
            previous_sql: Previous SQL query that failed
            error_message: Error message from the failed query
            
        Returns:
            Corrected SQL query
        """
        # Get schema information
        schema_info = self.get_related_schema(question)
        schema_text = "\n".join(schema_info)
        
        # Build prompt
        system_content = (
            "You are an expert SQL developer tasked with fixing SQL queries. "
            "You will be provided with a natural language question, a previous SQL query that failed, "
            "and the error message. Your job is to correct the SQL query to make it work."
        )
        
        if schema_text:
            system_content += f"\n\nDatabase Schema:\n{schema_text}"
        
        prompt = [
            self.system_message(system_content),
            self.user_message(
                f"Question: {question}\n\n"
                f"Previous SQL query (with error):\n{previous_sql}\n\n"
                f"Error message:\n{error_message}\n\n"
                f"Please provide a corrected SQL query that addresses the error. "
                f"Return only the SQL query without any explanation or additional text."
            )
        ]
        
        # Get response from Azure OpenAI
        sql = self.submit_prompt(prompt)
        
        # Clean up response
        sql = sql.strip()
        
        # Remove markdown code block formatting if present
        if sql.startswith("```sql"):
            sql = sql[6:]
        elif sql.startswith("```"):
            sql = sql[3:]
            
        if sql.endswith("```"):
            sql = sql[:-3]
            
        return sql.strip()
        
    def should_generate_visualization(self, df: pd.DataFrame) -> bool:
        """
        Determine if visualization should be generated for the DataFrame.
        
        Args:
            df: DataFrame to check
            
        Returns:
            True if visualization should be generated
        """
        # Only visualize if auto-visualization is enabled
        if not self.auto_visualization:
            return False
            
        # Don't visualize empty DataFrames
        if df.empty or len(df) == 0:
            return False
            
        # Don't visualize DataFrames with only one row and one column
        if df.shape == (1, 1):
            return False
            
        # Check if DataFrame has numeric columns suitable for visualization
        has_numeric = any(df[col].dtype.kind in 'ifc' for col in df.columns)
        
        # Minimum requirements: at least 2 rows or 2 columns, with at least one numeric column
        return (df.shape[0] >= 2 or df.shape[1] >= 2) and has_numeric
        
    def get_plotly_figure(self, plotly_code: str, df: pd.DataFrame) -> go.Figure:
        """
        Execute Plotly code to generate figure.
        
        Args:
            plotly_code: Python code for Plotly visualization
            df: DataFrame to visualize
            
        Returns:
            Plotly figure
        """
        # Create local namespace for execution
        local_vars = {"df": df, "go": go, "pd": pd}
        
        try:
            # Execute the generated code in the local namespace
            exec(plotly_code, globals(), local_vars)
            
            # Get the figure from local namespace
            fig = local_vars.get("fig")
            
            if fig is None:
                # Fallback: create a simple figure
                fig = go.Figure(data=go.Scatter(x=[0, 1], y=[0, 1], mode="markers"))
                fig.update_layout(title="Error: Visualization code did not produce a figure")
                
            return fig
            
        except Exception as e:
            # If execution fails, create an error figure
            fig = go.Figure(data=go.Scatter(x=[0, 1], y=[0, 1], mode="markers"))
            fig.update_layout(title=f"Error: {str(e)}")
            return fig

    def generate_data_summary(self, question: str, result: dict) -> str:
        """
        Generate a summary of the data using Azure OpenAI.
        
        Args:
            question: The original question
            result: Dictionary containing query results and SQL
            
        Returns:
            A natural language summary of the data
        """
        try:
            sql = result.get("sql", "")
            df = result.get("data")
            
            if df is None or len(df) == 0:
                return "No data returned from the query."
            
            # Create a prompt for OpenAI to summarize the data
            prompt = f"""

            You are an expert SQL developer who summarizes data and answers questions.

            You will be provided with a question, a SQL query, and a dataframe.

            Please provide a clear, concise summary of this data that directly answers the user's question, citing the tables and columns used to answer the question.
            
            Include key insights, trends, or patterns if relevant. Keep it brief and focused.

            Example of your task:
            <example>
            The user asked: How many teams are in the NBA?
            I ran the following SQL query: SELECT t.full_name, ROUND(AVG(gi.attendance), 0) as avg_attendance
                        FROM game g
                        JOIN game_info gi ON g.game_id = gi.game_id
                        JOIN team t ON g.team_id_home = t.id
                        WHERE gi.attendance > 0
                        GROUP BY t.id, t.full_name
                        ORDER BY avg_attendance DESC
                        LIMIT 1
            The query returned a dataframe with 1 rows and 2 columns.
            Column names: full_name, avg_attendance

            Here's the dataframe:
            full_name	avg_attendance
            18622	Toronto Raptors

            Assistant:
            The Toronto Raptors have the highest average attendance in the NBA with 18,622 fans per game, this was inferred using the table game_info and the column attendance.
            </example>

            The user asked: "${question}"
            
            I ran the following SQL query: ${sql}
            
            The query returned a dataframe with ${len(df)} rows and ${len(df.columns)} columns.
            Column names: ${', '.join(df.columns)}
            
            Here's the dataframe:
            ${df.head(10).to_string()}
            """
            
            prompt = [
                self.system_message(prompt),
                self.user_message("Please provide a concise summary of this data to answer the user's question.")
            ]
            
            # Get response from OpenAI
            summary = self.submit_prompt(prompt)
            
            return summary
            
        except Exception as e:
            print(f"Error generating data summary: {e}")
            import traceback
            traceback.print_exc()
            return f"Error generating summary: {str(e)}"
    
    def generate_starter_questions(self, schema: str, n=5) -> List[dict]:
        """
        Generate starter questions and visualization suggestions based on database schema.
        
        Args:
            schema: Database schema information
            n: Number of starter questions to generate
            
        Returns:
            List of question pairs as dictionaries
        """
        prompt = [
            self.system_message(
                f"You are a data analyst helping users explore a database. "
                f"The database has the following schema:\n\n{schema}"
            ),
            self.user_message(
                f"Generate {n} natural starter questions with corresponding visualization suggestions to help users explore this database. "
                f"Each question should be answerable with SQL and should provide insights about the data. "
                f"Make questions specific to the tables and columns in the schema.\n\n"
                f"For each question, also suggest an appropriate visualization type that would best display the results.\n\n"
                f"Return the output as a JSON array where each item has a 'question' field, each question should have a visualization suggestion"
                "An example output is [{'question': 'Treemap of Salary breakdown by team and then by position'}]"
            )
        ]
        
        response = self.submit_prompt(prompt)
        
        # Try to parse as JSON
        try:
            import json
            # Find JSON content (may be surrounded by markdown or other text)
            import re
            json_match = re.search(r'(\[.*\])', response.replace('\n', ' '), re.DOTALL)
            if json_match:
                questions_data = json.loads(json_match.group(1))
            else:
                # If no JSON array found, try to parse the whole response
                questions_data = json.loads(response)
                
            # Ensure we have the right format
            if isinstance(questions_data, list):
                # Clean up and standardize the format
                cleaned_data = []
                for item in questions_data:
                    if isinstance(item, dict) and 'question' in item:
                        cleaned_item = {
                            'question': item['question'],
                        }
                        cleaned_data.append(cleaned_item)
                return cleaned_data[:n]
            
        except Exception as e:
            # Fallback to simple text parsing if JSON parsing fails
            questions = [q.strip() for q in response.strip().split("\n") if q.strip()]
            return [{'question': q} for q in questions[:n]]