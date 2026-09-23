# ASM 1995 三元实验相图检索与阅览

Local search & viewer for **ASM 1995 Handbook of Ternary Alloy Phase Diagrams**.

ASM 1995 三元实验相图检索与阅览：按组分快速查找体系，并在界面中直接阅览相图。

> **版权**：相图图像 © 1995 ASM International。

## 安装与使用（推荐）

Windows 安装包已发布在本仓库的 **Releases** 页面，一般用户直接安装即可，无需配置 Python。

1. 打开本仓库页面，点击右侧（或顶部）的 **Releases**
2. 下载最新的安装包 `ASMTernaryPhaseFinder.exe*.exe`
3. 双击运行安装程序，按向导完成安装（可勾选创建桌面快捷方式）
4. 从开始菜单或桌面启动 **「ASM 三元实验相图检索」**
5. 在右侧选择材料 1 / 2 / 3（或使用快速输入，如 `Ag-Al-As`），点击 **显示相图**
6. 左侧即可阅览相图；结果列表中可切换同一体系的多张图

若系统提示未知发布者，可选择「仍要运行 / More info → Run anyway」（仅在确认安装包来自本仓库 Release 时）。

## 界面

![ASM 1995 三元实验相图检索与阅览界面](docs/screenshot-gui.png)

- **左侧**：相图阅览区，显示当前选中的实验相图
- **右侧**：材料 1 / 2 / 3 组分下拉选择、快速输入、`显示相图`、结果列表（同一体系若有多张图可切换）

## 查询

| 输入 | 含义 |
|------|------|
| 三个下拉框选 Al / As / Ag | 顺序无关 |
| 快速输入 `Ag-Al-As` | 精确三元体系 |
| `Fe Ni` | 含 Fe 与 Ni 的所有三元图 |

## 从源码运行（开发者）

```bat
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python.exe phase_finder.py gui
```

或双击 `打开相图检索.bat`。

## 仓库文件

| 文件 | 说明 |
|------|------|
| `phase_finder.py` | 主程序 |
| `phase_index.db` | 体系索引库（可选随仓库分发） |
| `docs/screenshot-gui.png` | 界面截图 |
| `GITHUB.md` | GitHub 名称/描述/上传清单 |
