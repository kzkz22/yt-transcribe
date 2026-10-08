import os
D = os.environ.get("FAKE_DIR", "/tmp")
def log(*a):
    with open(os.path.join(D, "calls.log"), "a") as f: f.write(" ".join(map(str, a)) + "\n")
def once(name):
    p = os.path.join(D, name)
    if os.path.exists(p):
        os.remove(p); return True
    return False
