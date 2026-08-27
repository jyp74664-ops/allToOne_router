import re
path = r"E:\workspace\model-gateway\api\proxy.py"
with open(path, "r", encoding="utf-8") as f:
    lines = f.readlines()
out = []
skip = False
for i, line in enumerate(lines):
    lineno = i + 1
    if lineno == 430:
        skip = True
        out.append("                # 记录失败并尝试下一候选（含 400 等非限流错误）\n")
        continue
    if skip:
        if lineno == 444:
            skip = False
            out.append("                continue\n")
        continue
    out.append(line)
with open(path, "w", encoding="utf-8") as f:
    f.writelines(out)
print("done")
