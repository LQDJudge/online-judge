# LQDOJ PROBLEM TAG


# **Coding Problem Difficulty Predictor**

This project is designed to predict the difficulty and associated tags for coding problems based on their descriptions using a chatbot API powered by Poe. The chatbot processes problem descriptions and returns predictions in the form of difficulty points (e.g., Codeforces rating) and relevant tags (e.g., `['greedy', 'implementation']`). The project handles multiple bot types (`GPT-4o`, `o3-mini`, `o1-mini`) and ensures robust error handling, logging, and retries for failed responses.

---

## **Features**

1. **Difficulty and Tag Prediction**:
   - Uses a chatbot to analyze coding problem descriptions and predict their difficulty rating and associated tags.

2. **Multiple Bot Compatibility**:
   - Works with different bot types like `GPT-4o`, `o3-mini`, and `o1-mini`.
   - Handles varied output formats, including intermediate messages like `"Thinking..."`.

3. **Error Handling and Retries**:
   - Retries failed requests up to 5 times.
   - Logs errors to a `log_errors.txt` file, including timestamps and raw responses for debugging.

4. **Response Sanitization**:
   - Ensures the chatbot's response is properly formatted and valid.
   - Sanitizes tags by wrapping unquoted tags (e.g., `greedy`) with quotes to avoid parsing errors.

5. **Rate Limit Compliance**:
   - Adds a 1-second delay between requests to avoid exceeding the API rate limit.

6. **Output Logging**:
   - Saves predictions to a `predictions.txt` file.

---

## **Project Structure**

```
.
├── listtagFull.txt         # File containing a list of valid tags (e.g., ['greedy', 'implementation', ...]).
├── listcode.txt            # File containing a list of problem codes (e.g., ['b1', 'b2', 'b3', ...]).
├── description/            # Directory containing problem descriptions (one file per problem, named <code>.txt).
│   ├── b1.txt
│   ├── b2.txt
│   └── ...
├── log_errors.txt          # Log file for failed attempts (created automatically if errors occur).
├── predictions.txt         # Output file containing the results (problem code, difficulty, and tags).
├── PredictProblem.py               # Main Python script that runs the project.
├── .env                    # Environment file containing the Poe API key.
└── README.md               # Documentation file describing the project.
```

---

## **How It Works**

### **1. Input Files**
- **`listtagFull.txt`**: Contains all valid tags (e.g., `['greedy', 'implementation', 'graph', ...]`).
- **`listcode.txt`**: Contains the list of problem codes (e.g., `['b1', 'b2', 'b3']`).
- **`description/<code>.txt`**: Individual text files for each problem, containing the problem description.

### **2. Main Script (`PredictProblem.py`)**
The script performs the following steps:
1. **Load Input Data**:
   - Reads tags from `listtagFull.txt`.
   - Reads problem codes from `listcode.txt` and loads their corresponding descriptions from the `description/` folder.

2. **Initialize Chatbot**:
   - Sends an initial "system" message to the chatbot, providing the list of valid tags and instructions on the response format.

3. **Process Problems**:
   - For each problem, sends the description to the chatbot and requests difficulty and tags.
   - Handles intermediate messages like `"Thinking..."` and extracts the final response.
   - Sanitizes the response to ensure it is valid Python syntax.

4. **Error Handling**:
   - Retries failed requests up to 5 times.
   - Logs errors (with timestamps and raw responses) to `log_errors.txt`.

5. **Save Results**:
   - Writes the predictions to `predictions.txt` in the format:
     ```
     ('b1', 800, ['greedy', 'implementation'])
     ('b2', 1300, ['graph', 'bfs'])
     ('b3', None, ['Error'])
     ```

---

## **How to Run the Project**

### **1. Prerequisites**
- Python 3.8 or higher.
- `fastapi-poe` library for interacting with the Poe API.
- API key for Poe (stored in a `.env` file).

### **2. Set Up the Environment**
1. Install the required Python libraries:
   ```
   pip install fastapi-poe python-dotenv
   ```
2. Create a `.env` file in the project directory:
   ```
   API_KEY=<your_poe_api_key>
   ```
   Replace `<your_poe_api_key>` with your Poe API key.

### **3. Prepare Input Files**
Ensure the following files are present:
- `listtagFull.txt`: Contains the list of valid tags.
- `listcode.txt`: Contains the list of problem codes.
- `description/<code>.txt`: Contains the problem descriptions.

### **4. Run the Script**
Run the main script:
```
python PredictProblem.py
```

### **5. Check the Output**
- **Predictions**: Check `predictions.txt` for the results.
- **Errors**: Check `log_errors.txt` for any failed attempts.

---

## **Example Workflow**

### **Input Files**
- `listtagFull.txt`:
  ```
  ['greedy', 'implementation', 'graph', 'dp', 'math']
  ```

- `listcode.txt`:
  ```
  ['b1', 'b2', 'b3']
  ```

- `description/b1.txt`:
  ```
  Find the maximum sum of a subarray in a given array of integers.
  ```

### **Output Files**
- **`predictions.txt`**:
  ```
  ('b1', 800, ['greedy', 'implementation'])
  ('b2', 1300, ['graph', 'bfs'])
  ('b3', None, ['Error'])
  ```

- **`log_errors.txt`**:
  ```
  [2025-02-17 15:00:00] Failed attempt 1 for problem b3: Thinking...Thinking...(None, [Error])
  [2025-02-17 15:00:01] Failed attempt 2 for problem b3: Thinking...(None, [Error])
  ```

---