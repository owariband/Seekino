# Chatbox 的设置怎样接到 Seekino

2026-11-07。源码基线为 Chatbox `d17455c5` 和 Seekino 本地 `kino-entry` 对应的 `59a124a0`。后者包含会话模型选择。播放器浮窗的淡化和停靠另有本机未提交改动。本文区分代码已有能力与建议的接法。[Chatbox 版本][chat-version] · [Seekino 版本][seek-version]

## 从聊天框换一次模型

Chatbox 的模型选择器负责展示并回传 `providerId / modelId`。在已有聊天里，调用方用 `updateSessionHeader` 写当前会话。生成时读取会话的有效设置，然后由模型工厂选择适配器。同一选择器放在「默认模型」页时，调用方写入的是相应功能的默认绑定。供应商页另管连接、模型列表和测试。「默认模型」页有管理供应商的入口。[选择器回调][chat-picker] · [会话写入][chat-session] · [生成适配器][chat-generation] · [默认模型页][chat-slots] · [供应商详情][chat-provider]

Chatbox 常规新聊天优先用显式配置的默认模型，其次是最近使用的模型，再回退到许可证提供的默认模型。打开已有聊天也可能更新“最近使用”。温度、top-p、上下文消息数等参数则支持单项继承，清除覆盖后重新跟随全局。Seekino 已确定“换模型只影响这个对话”。草稿从 `default` 开始，选择只存草稿，首次发送或上传时创建，已有会话只更新自身。选择器可以共用，保存动作由使用它的页面决定。[新建聊天][chat-new] · [最近使用与会话写入][chat-session] · [参数继承][chat-inherit] · [Seekino 草稿与选模][seek-hook]

Chatbox 管理模型的完整路径是选择供应商协议、填写连接信息、手动添加或获取模型，再把模型指定给某个用途。抓取列表、启用某个候选和点击「测试模型」是三个独立操作。普通 API Key 随 Chatbox 的应用设置在宿主侧存储。Seekino 的对应流程可以使用相同的界面顺序，凭据读取和供应商调用仍由 Server/Gateway 执行，聊天页只接收供选择的绑定标识。[添加供应商][chat-add] · [启用模型][chat-enable] · [普通 API Key 字段][chat-key] · [Seekino 模型配置][seek-model-doc]

## 设置布局

Chatbox 桌面常用设置入口在原页面上打开全屏设置层，内部可独立前进、后退。窄屏直接走设置路由。桌面分类导航宽 232px，右侧通用设置正文最大宽 760px。桌面、手机目录和设置搜索共享同一份分类表，搜索可以定位到具体字段。较长控件按**所在容器**排版：非触屏分组达到 560px 时，复杂控件可在 220px 标签列右侧显示，窄容器内上下排。普通开关仍可左右排列，触屏行至少 44px 高。[打开方式][chat-open] · [桌面设置层][chat-modal] · [导航与搜索][chat-nav] · [通用设置行][chat-rows] · [实际 CSS][chat-css]

Chatbox 的字段行接收标签和控件。实际控件订阅自己使用的设置值，修改时提交局部更新，`SettingsService` 合并、校验、通知订阅者，再排队持久化。普通偏好即时反馈，模型编辑框可以先保留草稿再点 Save。Seekino 可以用同样的受控控件接入现有 Provider 或 HTTP 客户端，服务端配置继续等待服务端确认后显示保存成功。[控件示例][chat-form] · [设置服务][chat-service] · [持久化队列][chat-persist] · [模型编辑框][chat-edit]

Seekino 目前用一个 `/settings` 路由连续展示外观、语言、连接及有权限的高级配置。建议用同一组「设置分区、设置行」来组织桌面内容、手机详情和 Kino 浮窗。沿用现有 React Router、CSS Modules 和 Seekino 的样式变量。桌面从聊天进入设置时保留当前聊天组件，手机进入独立详情时将草稿放到返回后能恢复的位置。当前输入文字保存在 `SearchPage` 的组件状态中，直接切换兄弟路由会卸载它。关闭设置应回到原文字、模型和光标位置，打开设置期间无需创建服务端会话。[现有路由][seek-routes] · [输入草稿][seek-draft] · [现有设置页][seek-page]

## Seekino 各类配置接到哪里

| 设置入口 | 已有的读写位置 | 建议的界面安排 |
| --- | --- | --- |
| 外观、语言、侧栏宽度 | 主题与语言由各自的 React Provider 保存到浏览器，聊天侧栏宽度也属浏览器偏好。[主题][seek-theme] · [语言][seek-locale] · [侧栏][seek-sidebar] | 放在「外观与语言」，修改立即生效。拖动侧栏继续直接改变宽度。 |
| 观看与 Kino | 观看页已有暂停视频、面板宽度偏好。本机浮窗还保存淡化秒数和停靠位置，淡化输入下限为 1 秒。[观看页][seek-watch] | 总设置放「观看与 Kino」。浮窗保留常用两项，两处接同一个偏好读写函数，沿用旧键。 |
| 账号、资料来源和连接状态 | `AuthProvider` 提供当前身份、资料来源及权限，设置页读取连接状态。[身份与来源][seek-auth] · [连接界面][seek-page] | 独立「账号与连接」。明确用户连的是本机还是远端资料库。模型供应商另列。 |
| 回复模型、当前对话 | 选项来自 `GET /api/agent/messages/options` 的 `chatModels`，已有会话用 `PATCH /api/agent/conversations/model`，草稿随原创建请求保存。服务端按会话选择本轮 Supervisor，记录实际模型名。[HTTP 客户端][seek-client-model] · [实际执行][seek-execute] | 聊天框写「此对话的模型」。把 `default` 显示成「跟随服务默认」并带上当前模型名，明确和显式选定某个绑定的区别。 |
| 本次回答参数 | Debug 下已有「本次回答设置」。运行轮次等覆盖可按身份和资料来源记在浏览器，发送时只在 Debug 把覆盖随消息提交。[身份范围][seek-identity-key] · [发送入口][seek-send] | 保留在输入框旁。提供恢复服务端默认的操作，并说明浏览器是否记住这个选择。 |
| 全局 Agent 运行参数 | 设置页有 `debug && canConfigure` 门槛，服务端另行验权。现成的 `GET/PATCH /api/agent/global-settings` 更新 `agent_runtime`，携带文件 revision，保存使用原子替换。并行任务上限有已保存值与生效值。[权限和保存][seek-global-api] · [版本与写入][seek-yaml] · [前端冲突反馈][seek-conflict] | 管理员「Agent 运行」复用原表单，保留显式保存、冲突重读、何时生效的反馈。 |
| 供应商、模型绑定、可选项 | YAML 的 `model_sources` 保存连接定义，`models` 定义绑定，`supervisor_choices` 开放聊天候选。启动时按 harness 和所需凭据装配工厂。普通用户的选项响应只有标识和名称。[配置说明][seek-model-doc] · [筛选][seek-choices] · [工厂装配][seek-boot] · [选项响应][seek-options] | 管理员「模型服务」编辑连接和可选绑定。「默认模型」展示各用途的选用结果。网页可直接展示现有聊天候选。编辑供应商、端点或角色绑定需要补服务端管理接口。 |
| 上传与解析 | 上传弹窗在 Debug 读取 `GET /api/uploads/parse-config`，允许使用默认值或为本次上传定制。任务保留配置快照，重试和重新解析有各自的合同。[上传表单][seek-upload] · [重试与重新解析][seek-reparse] | 总设置可以显示当前解析默认值。本次解析的修改仍放在上传或重新解析流程，显示适用的任务。 |

## 接入顺序

1. **先统一现有入口。** 在 `web/src/features/settings/` 组织分类、分区与字段行，使用现有的主题、语言、会话及全局设置读写方法。分类表供桌面导航和手机目录共用。搜索若加入，也按权限检索同一份字段目录。
2. **把用途与生效值说清楚。** 例如仓库配置同时声明了 `models.supervisor.reasoning_effort: low` 和 `agent_runtime.supervisor_reasoning_effort: none`。实际组装回答模型时优先取所选模型绑定的显式值。管理员表单写成功后，界面宜分别标明「全局默认」与「该模型固定为低」，避免把保存成功展示为模型已切换推理强度。[模型声明][seek-cfg-model] · [全局默认][seek-cfg-runtime] · [实际优先级][seek-reasoning]
3. **需要在网页新增供应商时，再补缺的链路。** 建议管理端读写经过筛选的连接元数据、模型绑定和开放选项，沿用管理员权限、revision 和原子写入。密钥由服务端引用，普通用户的聊天菜单继续消费 `chatModels`。现有工厂在启动时装配，因此先明确提示“保存后重启生效”。若以后要求立即生效，再增加工厂重载及运行中回答的隔离。可用凭据和兼容性检查只说明可以装配，真正的模型连通测试应是单独操作。[现有配置加载][seek-model-doc] · [现有装配][seek-boot] · [Chatbox 的模型测试动作][chat-test]

验收时可以用三个操作核对接入结果。先从一段未发出的草稿进入设置再返回，草稿仍在且会话数不增。再在聊天 A 改模型，聊天 B 与服务默认值保持原值。最后，管理员改运行参数后，界面显示服务端确认的值和实际生效时机。本次结论来自固定版本源码，未启动页面或调用模型。

[chat-version]: https://github.com/chatboxai/chatbox/commit/d17455c5ca8fd3ff44385c6d4d2118599e4c970f
[seek-version]: https://github.com/owariband/Seekino/commit/59a124a0321c7a41224c2b8b43e9e2f9c4004bbc
[chat-picker]: https://github.com/chatboxai/chatbox/blob/d17455c5ca8fd3ff44385c6d4d2118599e4c970f/src/renderer/components/ModelSelectorV2/index.tsx#L237-L247
[chat-session]: https://github.com/chatboxai/chatbox/blob/d17455c5ca8fd3ff44385c6d4d2118599e4c970f/src/renderer/routes/session/%24sessionId.tsx#L141-L175
[chat-generation]: https://github.com/chatboxai/chatbox/blob/d17455c5ca8fd3ff44385c6d4d2118599e4c970f/src/renderer/adapters/CurrentGenerationService.ts#L206-L228
[chat-slots]: https://github.com/chatboxai/chatbox/blob/d17455c5ca8fd3ff44385c6d4d2118599e4c970f/src/renderer/routes/settings/default-models.tsx#L257-L289
[chat-provider]: https://github.com/chatboxai/chatbox/blob/d17455c5ca8fd3ff44385c6d4d2118599e4c970f/src/renderer/routes/settings/provider/%24providerId.tsx#L288-L320
[chat-new]: https://github.com/chatboxai/chatbox/blob/d17455c5ca8fd3ff44385c6d4d2118599e4c970f/src/renderer/routes/index.tsx#L92-L99
[chat-inherit]: https://github.com/chatboxai/chatbox/blob/d17455c5ca8fd3ff44385c6d4d2118599e4c970f/src/renderer/stores/session/session-settings-inheritance.ts#L4-L54
[chat-open]: https://github.com/chatboxai/chatbox/blob/d17455c5ca8fd3ff44385c6d4d2118599e4c970f/src/renderer/modals/settings-navigation.ts#L4-L19
[chat-modal]: https://github.com/chatboxai/chatbox/blob/d17455c5ca8fd3ff44385c6d4d2118599e4c970f/src/renderer/modals/Settings.tsx#L218-L258
[chat-nav]: https://github.com/chatboxai/chatbox/blob/d17455c5ca8fd3ff44385c6d4d2118599e4c970f/src/renderer/routes/settings/route.tsx#L217-L406
[chat-rows]: https://github.com/chatboxai/chatbox/blob/d17455c5ca8fd3ff44385c6d4d2118599e4c970f/src/renderer/components/settings/SettingsLayout.tsx#L34-L96
[chat-css]: https://github.com/chatboxai/chatbox/blob/d17455c5ca8fd3ff44385c6d4d2118599e4c970f/src/renderer/static/index.css#L160-L225
[chat-test]: https://github.com/chatboxai/chatbox/blob/d17455c5ca8fd3ff44385c6d4d2118599e4c970f/src/renderer/routes/settings/provider/%24providerId.tsx#L403-L455
[chat-add]: https://github.com/chatboxai/chatbox/blob/d17455c5ca8fd3ff44385c6d4d2118599e4c970f/src/renderer/components/settings/provider/AddProviderModal.tsx#L25-L89
[chat-enable]: https://github.com/chatboxai/chatbox/blob/d17455c5ca8fd3ff44385c6d4d2118599e4c970f/src/renderer/routes/settings/provider/%24providerId.tsx#L1037-L1055
[chat-key]: https://github.com/chatboxai/chatbox/blob/d17455c5ca8fd3ff44385c6d4d2118599e4c970f/src/renderer/routes/settings/provider/%24providerId.tsx#L717-L743
[chat-form]: https://github.com/chatboxai/chatbox/blob/d17455c5ca8fd3ff44385c6d4d2118599e4c970f/src/renderer/routes/settings/chat.tsx#L95-L166
[chat-service]: https://github.com/chatboxai/chatbox/blob/d17455c5ca8fd3ff44385c6d4d2118599e4c970f/packages/chatbox-core/src/application/settings/SettingsService.ts#L80-L104
[chat-persist]: https://github.com/chatboxai/chatbox/blob/d17455c5ca8fd3ff44385c6d4d2118599e4c970f/packages/chatbox-core/src/application/settings/SettingsService.ts#L162-L170
[chat-edit]: https://github.com/chatboxai/chatbox/blob/d17455c5ca8fd3ff44385c6d4d2118599e4c970f/src/renderer/modals/ModelEdit.tsx#L247-L253
[seek-hook]: https://github.com/owariband/Seekino/blob/59a124a0321c7a41224c2b8b43e9e2f9c4004bbc/web/src/features/search/useAgentConversation.ts#L351-L386
[seek-routes]: https://github.com/owariband/Seekino/blob/59a124a0321c7a41224c2b8b43e9e2f9c4004bbc/web/src/app/App.tsx#L23-L33
[seek-draft]: https://github.com/owariband/Seekino/blob/59a124a0321c7a41224c2b8b43e9e2f9c4004bbc/web/src/features/search/SearchPage.tsx#L337-L352
[seek-page]: https://github.com/owariband/Seekino/blob/59a124a0321c7a41224c2b8b43e9e2f9c4004bbc/web/src/features/settings/SettingsPage.tsx#L216-L286
[seek-theme]: https://github.com/owariband/Seekino/blob/59a124a0321c7a41224c2b8b43e9e2f9c4004bbc/web/src/app/theme.tsx#L22-L37
[seek-locale]: https://github.com/owariband/Seekino/blob/59a124a0321c7a41224c2b8b43e9e2f9c4004bbc/web/src/app/i18n.tsx#L19-L39
[seek-sidebar]: https://github.com/owariband/Seekino/blob/59a124a0321c7a41224c2b8b43e9e2f9c4004bbc/web/src/features/search/SearchPage.tsx#L367-L401
[seek-watch]: https://github.com/owariband/Seekino/blob/59a124a0321c7a41224c2b8b43e9e2f9c4004bbc/web/src/features/player/WatchPage.tsx#L59-L81
[seek-auth]: https://github.com/owariband/Seekino/blob/59a124a0321c7a41224c2b8b43e9e2f9c4004bbc/web/src/features/auth/AuthProvider.tsx#L229-L243
[seek-client-model]: https://github.com/owariband/Seekino/blob/59a124a0321c7a41224c2b8b43e9e2f9c4004bbc/web/src/api/client.ts#L534-L556
[seek-execute]: https://github.com/owariband/Seekino/blob/59a124a0321c7a41224c2b8b43e9e2f9c4004bbc/server/service/agent_chat.py#L669-L725
[seek-identity-key]: https://github.com/owariband/Seekino/blob/59a124a0321c7a41224c2b8b43e9e2f9c4004bbc/web/src/api/client.ts#L211-L218
[seek-send]: https://github.com/owariband/Seekino/blob/59a124a0321c7a41224c2b8b43e9e2f9c4004bbc/web/src/features/search/SearchPage.tsx#L627-L647
[seek-global-api]: https://github.com/owariband/Seekino/blob/59a124a0321c7a41224c2b8b43e9e2f9c4004bbc/server/api/agent.py#L72-L110
[seek-yaml]: https://github.com/owariband/Seekino/blob/59a124a0321c7a41224c2b8b43e9e2f9c4004bbc/server/config/global_agent_settings.py#L50-L95
[seek-conflict]: https://github.com/owariband/Seekino/blob/59a124a0321c7a41224c2b8b43e9e2f9c4004bbc/web/src/features/settings/SettingsPage.tsx#L134-L146
[seek-model-doc]: https://github.com/owariband/Seekino/blob/59a124a0321c7a41224c2b8b43e9e2f9c4004bbc/docs/model-config.md#L3-L22
[seek-choices]: https://github.com/owariband/Seekino/blob/59a124a0321c7a41224c2b8b43e9e2f9c4004bbc/gateway/config.py#L512-L565
[seek-boot]: https://github.com/owariband/Seekino/blob/59a124a0321c7a41224c2b8b43e9e2f9c4004bbc/server/bootstrap.py#L463-L499
[seek-options]: https://github.com/owariband/Seekino/blob/59a124a0321c7a41224c2b8b43e9e2f9c4004bbc/server/service/agent_chat.py#L327-L344
[seek-upload]: https://github.com/owariband/Seekino/blob/59a124a0321c7a41224c2b8b43e9e2f9c4004bbc/web/src/features/uploads/UploadDialog.tsx#L279-L340
[seek-reparse]: https://github.com/owariband/Seekino/blob/59a124a0321c7a41224c2b8b43e9e2f9c4004bbc/server/service/upload.py#L268-L305
[seek-cfg-model]: https://github.com/owariband/Seekino/blob/59a124a0321c7a41224c2b8b43e9e2f9c4004bbc/config.yaml#L78-L88
[seek-cfg-runtime]: https://github.com/owariband/Seekino/blob/59a124a0321c7a41224c2b8b43e9e2f9c4004bbc/config.yaml#L157-L170
[seek-reasoning]: https://github.com/owariband/Seekino/blob/59a124a0321c7a41224c2b8b43e9e2f9c4004bbc/server/bootstrap.py#L420-L435
