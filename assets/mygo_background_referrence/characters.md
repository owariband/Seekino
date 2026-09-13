# MyGO!!!!! Character Bible

本文件是 AniVAG 在解析《BanG Dream! It's MyGO!!!!!》时使用的人物消歧背景。它只帮助模型把画面中的人物映射到稳定角色 ID，不能替代当前视频中的可见、可听证据。

<!-- ANIVAG_RUNTIME_CONTEXT_START -->

作品：《BanG Dream! It's MyGO!!!!!》。以下五人是人物消歧候选，不代表画面中的每个人都必须属于 MyGO!!!!!。

当画面特征、乐器或多人关系能够确认身份时，`events.characters` 与 `transcript.speaker` 必须使用下列标准名，不再使用“粉发女生”“黑长直女生”等临时外观称呼；证据不足时仍输出“未知人物”。

- 高松燈：灰紫色齐颈短发，粉红或红紫色眼睛；主唱，通常站在麦克风前且不持乐器。
- 千早愛音：粉色及腰长发，蓝灰或青绿色眼睛；吉他手，排练时常持白色或薄荷绿色电吉他。
- 要楽奈：银白色短发，一只眼偏金黄、另一只偏蓝；吉他手，常持红色电吉他。
- 長崎そよ：浅棕色及腰长发，蓝灰色眼睛；贝斯手，排练时常持红棕色贝斯。
- 椎名立希：深紫近黑色及腰长发，紫色眼睛，左眼下有泪痣；鼓手，演奏时位于鼓组后方。

时间字段必须使用毫秒：60 秒片段的末尾应写作约 `60000`，不能写成 `60`。

<!-- ANIVAG_RUNTIME_CONTEXT_END -->

## 使用规则

- 只有当发型、发色、服装、乐器位置或多人关系能够互相印证时，才输出角色真名。
- 角色会更换服装；发型、发色、脸部特征通常比单套服装更稳定，乐器和站位只能作为辅助证据。
- 无法可靠确认时保留“未知人物”或外观称呼，不要仅凭剧情预期强行命名。
- `official_profile` 是官方人物页信息的中文整理；`recognition_notes` 是根据官方立绘和动画画面总结的模型识别锚点。
- 本地立绘来自动画官网，仅用于本地研究与模型测试，不应重新发布或提交到公开仓库。

## 快速映射

| character_id | 标准名 | 常见查询别名 | 乐队职责 | 最强视觉锚点 | 本次基线模型的临时称呼 |
| --- | --- | --- | --- | --- | --- |
| `mygo.tomori` | 高松燈 | 高松灯、燈、灯、Tomori | 主唱 Vo. | 灰紫色短发、粉红或红紫色眼睛 | 紫发短发女生 |
| `mygo.anon` | 千早愛音 | 千早爱音、爱音、Anon | 吉他 Gt. | 粉色长发、蓝灰或青绿色眼睛 | 粉发长发女生 |
| `mygo.rana` | 要楽奈 | 要乐奈、乐奈、Rāna、Rana | 吉他 Gt. | 银白短发、左右眼异色 | 异瞳银发短发女生 |
| `mygo.soyo` | 長崎そよ | 长崎爽世、长崎素世、爽世、素世、Soyo、Soyorin | 贝斯 Ba. | 浅棕色长发、蓝色眼睛 | 棕发长发女生 |
| `mygo.taki` | 椎名立希 | 立希、Taki、Rikki | 鼓手 Dr. | 深紫近黑的长发、紫色眼睛、左眼下泪痣 | 黑长直女生 |

## `mygo.tomori` — 高松燈

![高松燈官方立绘](images/tomori_takamatsu.png)

- 日文名：高松 燈
- 英文名：Tomori Takamatsu
- 声优：羊宮妃那
- 乐队职责：主唱 `Vo.`，经常负责歌词和诗性表达。
- official_profile：羽丘女子学园高中一年级。对事物的感受方式与周围人不同，被称为“羽丘的不可思议女孩”。喜欢收集自己中意的东西，经常捡拾路上的石头和叶子。
- recognition_notes：灰紫色齐颈短发，发尾略微内扣，在不同光照下可能偏棕或蓝紫；粉红或红紫色眼睛；身形较小。常见校服是灰紫色西装外套、白衬衫、绿色领带和绿色格裙。舞台场景通常站在中央麦克风前，不持吉他。
- behavior_notes：说话较少、音量偏轻，紧张时容易停顿或蜷缩；谈到歌词、昆虫、石头或强烈感情时可能突然持续表达。她是 MyGO!!!!! 的主要情感和叙事中心之一。
- relation_notes：与爽世、立希同为旧乐队 CRYCHIC 的成员；立希通常明显保护和在意她。爱音主动接近她并推动新乐队形成。
- 不要混淆：不要因为短发就把她识别成乐奈；乐奈是银白发、异瞳，服装与行动也更加自由。
- 官方人物页：[anime.bang-dream.com/mygo/character/tomori](https://anime.bang-dream.com/mygo/character/tomori/)
- 官方立绘源：[body_tomori.png](https://anime.bang-dream.com/mygo/wordpress/wp-content/themes/mygo_v1/assets/images/common/character/body_tomori.png)

## `mygo.anon` — 千早愛音

![千早愛音官方立绘](images/anon_chihaya.png)

- 日文名：千早 愛音
- 英文名：Anon Chihaya
- 声优：立石凛
- 乐队职责：吉他 `Gt.`，也是推动成员聚到一起的关键行动者。
- official_profile：羽丘女子学园高中一年级。成绩优秀，社交能力和行动力强；初中时是班级人气人物并担任学生会长。性格追逐潮流，容易尝试当下流行的事物。
- recognition_notes：非常醒目的粉色长发，通常垂至腰部，部分场景会低位侧束；蓝灰或青绿色眼睛；表情和肢体动作丰富。与燈同校时常穿灰紫色外套、绿色领带和绿色格裙；便服也常保留粉色系视觉印象。排练时常持白色或薄荷绿色电吉他。
- behavior_notes：主动搭话、擅长调节尴尬气氛，常提出行动方案；想受到关注，有时会逞强或掩饰不安。吉他技术在故事早期并不稳定。
- relation_notes：是新乐队组建过程的推动者；经常给成员取昵称。她与立希容易发生言语冲突，但也会持续尝试维系团队。
- 不要混淆：五人中只有她是鲜明的粉色长发；多人同穿羽丘校服时，粉色长发是比服装更可靠的身份锚点。
- 官方人物页：[anime.bang-dream.com/mygo/character/anon](https://anime.bang-dream.com/mygo/character/anon/)
- 官方立绘源：[body_anon.png](https://anime.bang-dream.com/mygo/wordpress/wp-content/themes/mygo_v1/assets/images/common/character/body_anon.png)

## `mygo.rana` — 要楽奈

![要楽奈官方立绘](images/rana_kaname.png)

- 日文名：要 楽奈
- 英文名：Rāna Kaname
- 声优：青木陽菜
- 乐队职责：吉他 `Gt.`，演奏能力很强。
- official_profile：花咲川女子学园初中三年级，神出鬼没地出现在 Live House「RiNG」。吉他实力相当突出，明确表达喜欢与讨厌的事物，喜欢抹茶。
- recognition_notes：银白色短发；异色瞳，一只眼偏金黄、另一只偏蓝；服装风格比其他成员自由，官方立绘为宽松黑色露肩上衣和浅色短裤。演奏时常持红色或深红色电吉他。
- behavior_notes：行动像猫，随兴趣出现或离开；说话简短直接，常把人或音乐评价为“有趣/无聊”。即使周围人紧张，她也可能保持平静或只关注演奏。
- relation_notes：经常出现在 RiNG，与在那里工作的立希有交集；进入乐队的方式较自由，不完全服从其他成员的计划。
- 不要混淆：银白短发和异瞳组合在五人中唯一；即使没有拿吉他，也应优先依据眼睛和发色识别。
- 官方人物页：[anime.bang-dream.com/mygo/character/rana](https://anime.bang-dream.com/mygo/character/rana/)
- 官方立绘源：[body_rana.png](https://anime.bang-dream.com/mygo/wordpress/wp-content/themes/mygo_v1/assets/images/common/character/body_rana.png)

## `mygo.soyo` — 長崎そよ

![長崎そよ官方立绘](images/soyo_nagasaki.png)

- 日文名：長崎 そよ
- 中文查询别名：长崎爽世、长崎素世
- 英文名：Soyo Nagasaki
- 声优：小日向美香
- 乐队职责：贝斯 `Ba.`；在学校吹奏乐部负责低音提琴。
- official_profile：月之森女子学园高中一年级。平时气质温和，像可靠的大姐姐，对人友善并常被周围人依赖；思考时有摆弄手指的习惯。
- recognition_notes：浅棕色长发，发梢微卷；蓝色眼睛；气质柔和端正。月之森校服是深蓝色连衣裙式制服，白色领口和浅蓝领结非常醒目。舞台或排练场景通常持贝斯。
- behavior_notes：常微笑并用柔和语气协调关系，但在涉及 CRYCHIC、祥子或乐队存续时可能出现明显的执念、冷淡、愤怒或崩溃；“温和表情”和“真实情绪”需要分别记录。
- relation_notes：与燈、立希同为 CRYCHIC 旧成员，对旧乐队和丰川祥子有强烈情感牵引。这条背景可以用于解释情绪变化，但不能用于补写画面外动机。
- 不要混淆：她和立希都是长发；爽世是浅棕发、蓝眼和深蓝月之森制服，立希是深紫近黑长发、紫眼和花咲川制服。
- 官方人物页：[anime.bang-dream.com/mygo/character/soyo](https://anime.bang-dream.com/mygo/character/soyo/)
- 官方立绘源：[body_soyo.png](https://anime.bang-dream.com/mygo/wordpress/wp-content/themes/mygo_v1/assets/images/common/character/body_soyo.png)

## `mygo.taki` — 椎名立希

![椎名立希官方立绘](images/taki_shiina.png)

- 日文名：椎名 立希
- 英文名：Taki Shiina
- 声优：林鼓子
- 乐队职责：鼓手 `Dr.`，也承担较多编曲、排练要求和执行推进。
- official_profile：花咲川女子学园高中一年级，喜欢独处的一匹狼。在 Live House「RiNG」兼职，但不擅长接待客人；对燈明显宽容。她有一位年长四岁的姐姐。
- recognition_notes：深紫色、接近黑色的及腰长直发，部分造型会束发；紫色眼睛；左眼下有一颗泪痣。常见校服为浅棕色开衫、白色水手领上衣、红色领结和深色裙子。排练或演出时位于鼓组后方。
- behavior_notes：表情严肃、说话直接，容易因成员迟到、练习不足或伤害燈而发火；实际具有责任感并会照顾团队。面对燈时语气和态度通常比面对爱音柔和。
- relation_notes：与燈、爽世同为 CRYCHIC 旧成员；高度在意燈。她与爱音经常争执，也会对行踪自由的乐奈感到无奈。
- 不要混淆：不要把所有黑色长发角色都判为立希；需要紫色眼睛、花咲川制服、RiNG 工作服或鼓手位置中的至少另一项证据。
- 官方人物页：[anime.bang-dream.com/mygo/character/taki](https://anime.bang-dream.com/mygo/character/taki/)
- 官方立绘源：[body_taki.png](https://anime.bang-dream.com/mygo/wordpress/wp-content/themes/mygo_v1/assets/images/common/character/body_taki.png)

## 来源与版权

- 五人编制与乐队介绍：[BanG Dream! 官方 MyGO!!!!! Artist 页面](https://bang-dream.com/artist/mygo/)
- 动画角色总览：[《BanG Dream! It's MyGO!!!!!》官方角色页](https://anime.bang-dream.com/mygo/character/)
- 人物基础资料与立绘：上述各角色官方人物页。
- 官方页面标注相关图片、照片和插画禁止未经授权复制或转载；本目录中的图片只用于 AniVAG 本地研究和模型能力验证，并已通过 `.gitignore` 排除。
