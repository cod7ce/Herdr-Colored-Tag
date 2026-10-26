# Herdr Colored Tag

版本 **0.1.0**。终端版 Herdr 的纯插件颜色管理：**同标签的父 Space 相邻排列，原生标题使用相同颜色**；Agents 列表里的所属 Space 名称也跟随标签色。原生 worktree 组作为整体移动，保留原有缩进、折叠与名称。不修改 Herdr / GPUI，不创建父 Space，不添加树形前缀、标签文本或额外标题行。

## 安装与使用

需要 Herdr 0.9.5+、Python 3.9+（标准库，包含 curses），macOS 或 Linux。在 Herdr 管理的终端执行：

```sh
herdr plugin install cod7ce/Herdr-Colored-Tag
herdr plugin action invoke local.herdr-colored-tag.manage
```

需要修改源码时，也可以克隆并链接本地目录：

```sh
git clone https://github.com/cod7ce/Herdr-Colored-Tag.git
cd Herdr-Colored-Tag
herdr plugin link "$PWD"
```

已安装但停用时：

```sh
herdr plugin enable local.herdr-colored-tag
```

管理弹窗按标签分区，侧栏直接改变父 Space 的标题颜色。每个父 Space / 原生整组只使用一个排序标签，未设置标签的 Space 使用原生颜色。Spaces 区的 linked worktree 子项保留原生颜色；选择其 ID 设置标签时会处理所属整组。

| 按键 | 操作 |
| --- | --- |
| ↑ / ↓ 或 j / k | 选择父 Space / 原生整组 |
| Space | 多选或取消选择 |
| t | 为所选项目设置已有标签，或输入新标签并选颜色 |
| c | 修改当前标签颜色，应用到该标签的所有成员 |
| d | 移除所选项目的标签，恢复原生颜色 |
| e | 修改标签名称 |
| s | 打开标签排序面板 |
| r | 重命名所选 Space |
| g | 同步颜色和排列并刷新 |
| Enter | 聚焦所选父 Space 并关闭弹窗 |
| q / Esc | 关闭弹窗 |

未多选时 `t` / `d` 作用于当前项。多选标记保留以便继续操作，按 Space 取消。支持蓝、绿、橙、紫、青、红、黄、灰八种颜色。管理界面的标签标题方便辨认分组，不会在原生侧栏插入额外行。

Action：`local.herdr-colored-tag.manage` 打开管理；`local.herdr-colored-tag.tag` 打开设置提示；`local.herdr-colored-tag.detach` 移除调用方整组标签；`local.herdr-colored-tag.sync` 同步。

## CLI

以下命令在插件目录执行；`w1`、`w2` 是示例，请使用 `herdr workspace list` 返回的 ID。

```sh
python3 herdr_group.py tag "性能测试" w1 w2 --color blue
python3 herdr_group.py untag w1
python3 herdr_group.py list
python3 herdr_group.py tag-edit TAG_ID --color purple
python3 herdr_group.py rename w1 "新的名称"
python3 herdr_group.py sync
python3 herdr_group.py reset
```

同名标签会复用；显式指定颜色会更新该标签的所有成员。标签默认按首次创建顺序排列，可在管理面板按 `s` 打开标签排序：用 `↑/↓` 或 `j/k` 选择标签，按大写 `K`（Shift+k）上移、`J`（Shift+j）下移，`Enter` / `q` / `Esc` 返回。每次调整立即保存并同步侧栏，重启后保留；列表包含暂未分配 Space 的标签，新建标签追加在最后。

排序移动标签下的所有 Space / 原生 worktree 整组，成员保留当前相对顺序；未标记项固定在最后。排序面板的返回键不撤销已经保存的调整。移除最后一个标签时保留当前顺序。

## Agents 列表中的 Space 标签色

插件同时给 Agents 列表中 agent 所属的 Space 名称应用相同标签色，保留原生 agent 名称与状态图标。规则按 Space 的 workspace label 匹配；worktree 子 Space 使用自己的 label，并跟随所属整组的标签。Space 改名、改标签色、清除标签或关闭时，相关规则会在下一次插件同步后更新。规则预先覆盖所有已标记 Space（包含 worktree 子项），因此新 agent 出现后即可匹配，无需等待 agent 事件同步。

Herdr 0.9.5 的颜色规则按 token 自身文字匹配，不能按 workspace ID 或相邻 agent 名称判断。同名 Space 若要使用不同标签颜色会报冲突；同色则可共用规则。Spaces 和 Agents 的 workspace token **各自最多 16 条规则**；Agents 的上限包含已标记父项及其 worktree 子项的不同 label，尚未运行 agent 的 Space 也计入。Agent 名称和运行状态颜色仍由原生配置绘制。现存 Agents 自定义布局与插件布局冲突时，插件会停止并保留原配置，不自动覆盖。

配色位置与能力边界见 [交互说明图](docs/color-map.html)；下载后用浏览器打开 HTML 即可离线查看。

## 原生标题颜色的实现与边界

插件在 `$XDG_CONFIG_HOME/herdr/config.toml`（默认 `~/.config/herdr/config.toml`）维护带标记的 Spaces 与 Agents 颜色块，为原生 `workspace` token 设置精确标题匹配的 `fg` 规则，并通过 `server.reload_config` 即时应用。Spaces 保留标准 `state_icon`、`workspace`、`branch`、`git_status` 显示。Agents 保留标准 `state_icon`、`machine`、`workspace`、`tab` 和 `agent` 显示。标题始终由 Herdr 生成，未使用元数据替代标题。

- Herdr 0.9.5 每个 token 最多允许 **16 条颜色规则**，因此 Spaces 区最多为 16 个不同的父 Space 标题设置颜色。超过任一区域上限时拒绝本次设置，不静默漏掉项目。
- 规则按标题文本匹配，不按 Space ID。同名项目必须使用同色，否则拒绝本次设置；可先给它们起不同名称。同名 linked 子项也可能与父项规则冲突。Git 自动生成的子项标题可能与 API 名称不同，此类同名请自行区分。
- emoji 本身通常由终端作为彩色字形绘制；标题文字着色不代表会改变 emoji 的固有颜色。
- 颜色配置属于 Herdr 配置文件，共享它的客户端会看到相同规则。共享配置的命名会话中只允许一个会话管理颜色块；其他会话设置颜色会提示冲突，不会覆盖。独立 XDG 配置可分别管理。
- 如果已有自定义 `[ui.sidebar.spaces]` 或相关 dotted 配置，插件会提示冲突并保留原配置，不自动覆盖。
- 收起侧栏 / 移动端布局不一定使用这些自定义行样式。

配置变更前自动备份，使用文件锁和原子替换。两段颜色配置分别应用；某段重载失败时回滚该次写入，不会撤销此前已成功应用的另一段。修复报错原因后可用 `sync` 重试。若人工编辑了插件管理的块，会提示冲突；改颜色请通过管理弹窗。

兼容旧配置时，如果相邻颜色块共享同一个分隔换行、且分别属于不同会话，清除前面的块可能被拒绝，以免改动另一会话的配置。可先由对应会话清除后面的块再重试；0.1.0 新生成的块各自管理分隔换行，不受此限制。

## 持久化与原生 worktree

标签状态按 socket 路径区分会话，存于 `HERDR_PLUGIN_STATE_DIR`，直接 CLI 使用 `$XDG_STATE_HOME/herdr/plugins/local.herdr-colored-tag`（默认 `~/.local/state/herdr/plugins/local.herdr-colored-tag`）。事件钩子在 Space 创建、关闭、改名、移动和 worktree 创建/打开后同步；启动钩子恢复标签。重复同步不重复改名、写配置或排序。

插件 ID 从 `local.herdr-group` 改为 `local.herdr-colored-tag` 后，所有 Herdr action、启用及停用命令使用新 ID。从旧 ID 升级时，应在插件停用期间将旧状态目录复制到新目录并保留原目录作为备份；新目录已有数据时需先核对，不能直接覆盖。插件不会自动读取旧 ID 的目录。颜色配置块保留 `local.herdr-group` 内部标记，以兼容已有配置及其完整性校验；该标记不影响新命令。

选择原生主 Space 或 linked worktree 都操作整组。同仓库重复打开的主项保持同色；新增 linked 成员跟随主项，关闭代表主项后标签转移到存活成员。原生组解散时，剩余独立 Space 保留标签。插件不关闭 Space、不删除 checkout，也不改原生 worktree 元数据。

旧 v1 状态会备份为 `groups-<hash>.v1-backup.json` 后迁移；有旧父子关系时转为顶层同名标签并清理旧树形前缀。**已经 reset 的关系保持为空，不会从备份自动恢复。** 新版不再提供 `attach`、`create`、`create-group` 命令，也不会建立分组用的空 Space；`detach` 是 `untag` 的兼容别名。

## 停用与恢复

停用插件会停止同步，现有标题颜色和排列会保留，因为颜色规则已写入 Herdr 配置。即使插件未运行或被卸载，原生标题也始终显示。重新启用可继续使用标签。

要同时清除标签和颜色，在插件目录先运行 `reset` 再停用：

```sh
python3 herdr_group.py reset
herdr plugin disable local.herdr-colored-tag
```

`reset` 删除手动标签和插件拥有的两段颜色配置块，恢复原生颜色；不恢复历史排列，不删除任何 Space 或目录。插件停用时仍可直接运行该命令。旧版本创建的父 Space 由用户自行决定是否保留。

## 验证

```sh
python3 -m unittest discover -s tests -v
python3 tests/integration.py
```

集成测试需要 Python 3.11+、Herdr 和 tmux，并须从 Herdr 管理的终端运行。测试使用独立 XDG 配置、一次性 Herdr 命名会话和独立 tmux 服务，实际驱动弹窗、读取 ANSI 标题颜色、验证原生 worktree 折叠、重命名、生命周期、重启和 reset。测试创建两条同名 agent 的生命周期记录，专门从 Agents 区读取 Space 名称的颜色，验证跨标签配色、linked 子项继承、改色和改名；不会启动或调用 AI 模型。reset 检查配置逐字节恢复，包括原文件没有末尾换行的情况。只清理自己创建的测试资源。
