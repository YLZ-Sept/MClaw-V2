import os

# Try to find the logo directory
paths_to_try = [
    "E:/桌面/LLM的图标设计 (2)",
    "E:/Desktop/LLM的图标设计 (2)",
]

for p in paths_to_try:
    if os.path.isdir(p):
        print(f"FOUND: {p}")
        for f in os.listdir(p):
            print(f"  {f}")
        break
else:
    # Walk E: drive top level
    try:
        for item in os.listdir("E:/"):
            print(f"E:/{item}")
    except:
        print("E: drive not found")
