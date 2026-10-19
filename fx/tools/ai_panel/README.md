# ai_panel：多模型专家面板

用途：对不确定的策略参数，向多个独立 AI 模型各问同一道题，把原始回答保存在本地，再由一个汇总模型提取共识、分歧和最不确定之处（Expert Elicitation）。回答只是各模型的意见，没有经过回测，不能当成结论。

## 环境变量
```bash
export PANEL_API_KEY=你的中转站密钥        # 必需，只从环境变量读取，不写入代码和结果
export PANEL_BASE_URL=https://api.novaapi.ai/v1   # 可选，默认即此值
```

## 三个命令
```bash
# 1) 只跑部分题目，结果保存到 results/日期/
python -m fx.tools.ai_panel.panel --run --questions "A1_*.txt"
# 2) 重新调用已有结果的模型 / 只跑指定模型
python -m fx.tools.ai_panel.panel --run --questions "A1_*.txt" --models glm-5.2 --force
# 3) 读本地结果做汇总（不会再调用被测模型，只调用一次汇总模型）
python -m fx.tools.ai_panel.panel --analyze --results fx/tools/ai_panel/results/2026-10-20/
```
增量逻辑：已有成功结果的模型默认跳过，加 `--force` 才重新调用。回答为空或出错的模型下次运行会自动补跑。

## 新增问题
在 `questions/` 里新建 `编号_主题_角色.txt`，内容只写问题本身（不要写模型相关内容）。文件名（不含扩展名）就是结果文件名。

## 配置
`config.py`：模型列表（name / max_tokens / token_param）、系统提示、汇总模型、目录。
- 推理型模型（o 系列、glm-5.x）会先消耗思考 token，`max_tokens` 要给足（>=3000），否则会返回空回答且照常计费。
- `gpt-6*` 与 o 系列自动使用 `max_completion_tokens`。
- 重试：失败最多重试 2 次、间隔 3 秒；402/403/404 等不会重试，避免白花钱。

## 费用提示
费用主要来自模型输出长度。每次运行前先用 1 道题试跑核对扣费，再放开全部。

## 注意
- 问题内容会发送到第三方中转站，涉及策略细节时请自行评估是否可以外发。
- `C1_权重联动_哲学.txt` 是按片段重建的版本，使用前请核对。
- `results/` 含模型原始回答，已在 `.gitignore` 中忽略，不会提交。
