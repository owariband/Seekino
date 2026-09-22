# MyGO!!!!! Character Bible

本文件是 anivag 在解析《BanG Dream! It's MyGO!!!!!》时使用的人物消歧背景。它只帮助模型把画面中的人物映射到稳定角色 ID，不能替代当前视频中的可见、可听证据。

<!-- ANIVAG_RUNTIME_CONTEXT_START -->

作品：《BanG Dream! It's MyGO!!!!!》。以下九人是人物消歧候选：MyGO!!!!! 的五位成员，以及若葉睦、豊川祥子、三角初華、八幡海鈴四位重要关联人物。不能把关联人物自动归为 MyGO!!!!! 成员，也不能假定画面中的每个人都在候选名单内。

当画面特征、乐器或多人关系能够确认身份时，`events.characters` 与 `transcript.speaker` 必须使用下列标准名，不再使用“粉发女生”“黑长直女生”等临时外观称呼；证据不足时仍输出“未知人物”。

- 高松燈：灰紫色齐颈短发，粉红或红紫色眼睛；主唱，通常站在麦克风前且不持乐器。
- 千早愛音：粉色及腰长发，蓝灰或青绿色眼睛；吉他手，排练时常持白色或薄荷绿色电吉他。
- 要楽奈：银白色短发，一只眼偏金黄、另一只偏蓝；吉他手，常持红色电吉他。
- 長崎そよ：浅棕色及腰长发，蓝灰色眼睛；贝斯手，排练时常持红棕色贝斯。
- 椎名立希：深紫近黑色及腰长发，紫色眼睛，左眼下有泪痣；鼓手，演奏时位于鼓组后方。
- 若葉睦：浅绿色长发，金黄或琥珀色眼睛；原 CRYCHIC 吉他手，月之森女子学园高一学生，与祥子是青梅竹马，与爽世同班。
- 豊川祥子：浅蓝或蓝紫色长发，侧束部分头发并饰黑色蝴蝶结，金黄或琥珀色眼睛；原 CRYCHIC 键盘手兼作曲者，现为羽丘女子学园高一学生。回忆中的学校与服装可能不同。
- 三角初華：浅金色齐肩发，紫色眼睛；花咲川女子学园高一学生，双人偶像组合 sumimi 的吉他手，负责作词、作曲，与祥子自幼相识。
- 八幡海鈴：黑色齐肩发，发尾略向外翘，青绿或蓝绿色眼睛；花咲川女子学园高一学生，为多个乐队提供贝斯支援，与立希、初华同班。

文档中标为 `later_reference` 的 Ave Mujica 身份与舞台名只用于跨作品查询和人物消歧。解析当前集时，人物的乐队归属、关系变化和发生的事件必须以当前视频及已解析的前序内容为准，不能把后期设定提前写入证据。

时间字段必须使用毫秒：60 秒 VideoChunk 的末尾应写作约 `60000`，不能写成 `60`。

<!-- ANIVAG_RUNTIME_CONTEXT_END -->

## 使用规则

- 只有当发型、发色、服装、乐器位置或多人关系能够互相印证时，才输出角色真名。
- 角色会更换服装；发型、发色、脸部特征通常比单套服装更稳定，乐器和站位只能作为辅助证据。
- 无法可靠确认时保留“未知人物”或外观称呼，不要仅凭剧情预期强行命名。
- `official_profile` 是官方人物页信息的中文整理；`recognition_notes` 是根据官方立绘和动画画面总结的模型识别锚点。
- 本地立绘为动画官方角色形象；原五张直接来自动画官网，新增四张来自社区保存的立绘镜像，来源在对应角色下标明。仅用于本地研究与模型测试，不应重新发布或提交到公开仓库。

## 快速映射

| character_id | 标准名 | 常见查询别名 | 乐队职责 | 最强视觉锚点 | 本次基线模型的临时称呼 |
| --- | --- | --- | --- | --- | --- |
| `mygo.tomori` | 高松燈 | 高松灯、燈、灯、Tomori | 主唱 Vo. | 灰紫色短发、粉红或红紫色眼睛 | 紫发短发女生 |
| `mygo.anon` | 千早愛音 | 千早爱音、爱音、Anon | 吉他 Gt. | 粉色长发、蓝灰或青绿色眼睛 | 粉发长发女生 |
| `mygo.rana` | 要楽奈 | 要乐奈、乐奈、Rāna、Rana | 吉他 Gt. | 银白短发、左右眼异色 | 异瞳银发短发女生 |
| `mygo.soyo` | 長崎そよ | 长崎爽世、长崎素世、爽世、素世、Soyo、Soyorin | 贝斯 Ba. | 浅棕色长发、蓝色眼睛 | 棕发长发女生 |
| `mygo.taki` | 椎名立希 | 立希、Taki、Rikki | 鼓手 Dr. | 深紫近黑的长发、紫色眼睛、左眼下泪痣 | 黑长直女生 |
| `mygo.mutsumi` | 若葉睦 | 若叶睦、睦、Mutsumi、Mortis | 原 CRYCHIC 吉他 Gt. | 浅绿长发、金黄或琥珀色眼睛、月之森制服 | 未记录 |
| `mygo.sakiko` | 豊川祥子 | 丰川祥子、豐川祥子、祥子、Sakiko、Saki、Oblivionis | 原 CRYCHIC 键盘 Key.、作曲 | 蓝紫长发、侧束黑色蝴蝶结、金黄或琥珀色眼睛 | 未记录 |
| `mygo.uika` | 三角初華 | 三角初华、初華、初华、Uika、Doloris | sumimi 吉他、作词、作曲 | 浅金色齐肩发、紫色眼睛 | 未记录 |
| `mygo.umiri` | 八幡海鈴 | 八幡海铃、海鈴、海铃、Umiri、Timoris | 支援贝斯 Ba. | 黑色齐肩发、外翘发尾、青绿或蓝绿色眼睛 | 未记录 |

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

## `mygo.mutsumi` — 若葉睦

![若葉睦角色立绘（社区镜像）](images/mutsumi_wakaba.webp)

- 日文名：若葉 睦
- 中文查询别名：若叶睦、睦
- 英文名：Mutsumi Wakaba
- 声优：渡瀬結月
- 乐队职责：原 CRYCHIC 吉他手 `Gt.`。
- official_profile：月之森女子学园高中一年级。很少通过语言或表情显露情绪。父亲是知名喜剧演员若葉，母亲是演员森みなみ；从小接触吉他，演奏水平很高。
- recognition_notes：浅绿色长发，平齐或略分束的刘海，金黄或琥珀色眼睛，常带细小发夹；常见学校场景为深蓝色月之森制服，与爽世同校。校服与沉默表情只能作辅助线索，需要结合发型、脸部、人物参考图和明确对话确认身份。
- behavior_notes：发言简短、表情变化较少，但沉默不能直接解释为冷漠或不在意。不要在没有对话或行动证据时补写她的内心想法。
- relation_notes：与祥子是青梅竹马，与爽世同班；与燈、爽世、立希、祥子同为 CRYCHIC 的旧成员。她与旧成员的联系是消歧背景，不能据此假定当前场景一定在谈论旧乐队。
- later_reference：在后续 Ave Mujica 中担任吉他手，舞台名 `Mortis`（モーティス）。该身份不代表 MyGO 故事的每个时间点都已加入 Ave Mujica。
- 不要混淆：月之森制服不是爽世独有，睦是浅绿色长发而爽世是浅棕色长发；不要因光照下发色较浅便判为银白短发、异瞳的乐奈。
- 官方人物页：[MyGO!!!!!：若葉睦](https://anime.bang-dream.com/mygo/character/mutsumi/)
- 官方补充资料：[Ave Mujica：若葉睦](https://anime.bang-dream.com/avemujica/character/mutsumi/)
- 立绘镜像：[Mutsumi_anime.png](https://raw.githubusercontent.com/Furinaaa-Cancan/mygo-mujica-archive/main/assets/AveMujica/art/cards-wikia/Mutsumi/Mutsumi_anime.png)

## `mygo.sakiko` — 豊川祥子

![豊川祥子角色立绘（社区镜像）](images/sakiko_togawa.webp)

- 日文名：豊川 祥子
- 中文查询别名：丰川祥子、豐川祥子、祥子
- 英文名：Sakiko Togawa
- 声优：高尾奏音
- 乐队职责：原 CRYCHIC 键盘手 `Key.`，负责作曲。
- official_profile：羽丘女子学园高中一年级。曾在 CRYCHIC 演奏键盘并创作歌曲，经常在学校音乐教室弹钢琴。言谈带有端正、礼貌的大小姐风格。
- recognition_notes：浅蓝或蓝紫色长发，部分头发在两侧束起并饰黑色蝴蝶结，发尾微卷；金黄或琥珀色眼睛。当前时间线常见羽丘制服，与燈、爱音同校；回忆中的校服可能不同。钢琴与键盘可以辅助判断，但不能把画面中所有弹琴者直接识别成祥子。
- behavior_notes：说话和举止较端正，面对旧乐队相关话题可能呈现明显的情绪与态度变化。必须分别记录当前可见表现与背景关系，不根据家世、后续剧情或他人的执念补写她的动机。
- relation_notes：与睦是青梅竹马，与初华自幼相识；与燈、爽世、立希、睦同为 CRYCHIC 的旧成员。爽世对她和旧乐队有强烈情感牵引，但是否正在联络、争执或疏远必须由当前片段判断。
- later_reference：后续组建 Ave Mujica 并担任键盘手，舞台名 `Oblivionis`（オブリビオニス）。不能把后期组建乐队的事实提前当成早期视频事件。
- 不要混淆：羽丘制服不是爱音或燈独有；祥子的蓝紫长发、侧束蝴蝶结与金黄眼睛应与爱音的粉色长发、燈的灰紫短发区分，乐器只作辅助线索。
- 官方人物页：[MyGO!!!!!：豊川祥子](https://anime.bang-dream.com/mygo/character/sakiko/)
- 官方补充资料：[Ave Mujica：豊川祥子](https://anime.bang-dream.com/avemujica/character/sakiko/)
- 立绘镜像：[Sakiko_anime.png](https://raw.githubusercontent.com/Furinaaa-Cancan/mygo-mujica-archive/main/assets/AveMujica/art/cards-wikia/Sakiko/Sakiko_anime.png)

## `mygo.uika` — 三角初華

![三角初華角色立绘（社区镜像）](images/uika_misumi.webp)

- 日文名：三角 初華；MyGO 动画人物页也以「初華」列出。
- 中文查询别名：三角初华、初華、初华
- 英文名：Uika Misumi
- 声优：佐々木李子
- 乐队职责：双人偶像组合 `sumimi` 的吉他手，负责作词、作曲。
- official_profile：花咲川女子学园高中一年级。作为 sumimi 成员活动，以酷的形象受到欢迎；喜欢观察星星。组合的另一位成员是まな，不能把双人组合中每位成员都识别为初华。
- recognition_notes：浅金色齐肩发，发尾微内扣，紫色眼睛；参考立绘为黑白配色的无袖演出裙、白色领带和腰带。可能出现在 sumimi 的演出、宣传画面或日常学校场景中；与立希、海铃同穿花咲川制服时，服装无法单独区分身份。应结合发型、脸部、参考图及明确称呼确认。
- behavior_notes：可通过音乐、歌词或星空话题与人交流，但“喜欢星星”不能被当作任何星空场景中一定出现她的证据。
- relation_notes：与祥子自幼相识，与立希、海铃同班；在 MyGO 剧情中与燈有交集。具体相遇时间和对话内容必须来自当前集或已解析的前序内容，不据背景文档补写。
- later_reference：在后续 Ave Mujica 中担任吉他兼主唱 `Gt. & Vo.`，舞台名 `Doloris`（ドロリス）。不要把 sumimi 时期的镜头自动标为 Ave Mujica 活动。
- 不要混淆：sumimi 画面中的まな是另一位人物；初华是浅金色齐肩发、紫眼，与燈的灰紫短发、乐奈的银白短发异瞳不同。主唱位置或花咲川校服都不是她独有的身份条件。
- 官方人物页：[MyGO!!!!!：初華](https://anime.bang-dream.com/mygo/character/uika/)
- 官方补充资料：[Ave Mujica：三角初華](https://anime.bang-dream.com/avemujica/character/uika/)
- 官方英文姓名：[BanG Dream! 官方 MyGO!!!!! 英文人物介绍](https://en.bang-dream.com/anime/mygo/)
- 立绘镜像：[Uika_anime.png](https://raw.githubusercontent.com/Furinaaa-Cancan/mygo-mujica-archive/main/assets/AveMujica/art/cards-wikia/Uika/Uika_anime.png)

## `mygo.umiri` — 八幡海鈴

![八幡海鈴角色立绘（社区镜像）](images/umiri_yahata.webp)

- 日文名：八幡 海鈴
- 中文查询别名：八幡海铃、海鈴、海铃
- 英文名：Umiri Yahata
- 声优：岡田夢以
- 乐队职责：支援贝斯手 `Ba.`。
- official_profile：花咲川女子学园高中一年级。具有接近专业水平的贝斯演奏能力，同时为约三十支乐队提供支援。对演奏和工作有认真、专业的态度。
- recognition_notes：黑色齐肩发，斜分刘海，发尾略向外翘；青绿或蓝绿色眼睛。学校场景常见花咲川制服，与立希、初华同校同班；演奏时贝斯可以作辅助线索。需要结合发型、脸部、参考图和明确对话，不能把所有贝斯手都归为海铃。
- behavior_notes：与立希相处时常以淡定的态度开玩笑或逗她；具体语气、动作和两人的情绪仍要依当前视频判断，不能把互动直接推断成冲突或亲密关系。
- relation_notes：与立希、初华同班。她会为不同乐队提供演出支援，因此出现在某个乐队的演奏现场不自动等于已成为该乐队的固定成员。
- later_reference：在后续 Ave Mujica 中担任贝斯手，舞台名 `Timoris`（ティモリス）。与早期支援活动分开判断，不预设当前视频中的固定归属。
- 不要混淆：海铃与爽世都演奏贝斯，但海铃是黑色齐肩发而爽世是浅棕色长发；与立希同穿花咲川制服时，应区分海铃的较短发型和青绿眼睛，以及立希的长发、紫眼和泪痣。
- 官方人物页：[MyGO!!!!!：八幡海鈴](https://anime.bang-dream.com/mygo/character/umiri/)
- 官方补充资料：[Ave Mujica：八幡海鈴](https://anime.bang-dream.com/avemujica/character/umiri/)
- 立绘镜像：[Umiri_anime.png](https://raw.githubusercontent.com/Furinaaa-Cancan/mygo-mujica-archive/main/assets/AveMujica/art/cards-wikia/Umiri/Umiri_anime.png)

## 来源与版权

- 五人编制与乐队介绍：[BanG Dream! 官方 MyGO!!!!! Artist 页面](https://bang-dream.com/artist/mygo/)
- 动画角色总览：[《BanG Dream! It's MyGO!!!!!》官方角色页](https://anime.bang-dream.com/mygo/character/)
- 四位关联人物的后续身份与关系补充：[《BanG Dream! Ave Mujica》官方角色页](https://anime.bang-dream.com/avemujica/character/)。后续信息仅作消歧参考，不能替代当前视频证据。
- 人物基础资料与立绘：上述各角色官方人物页。
- 新增四张立绘经 [mygo-mujica-archive 的 cards-wikia 分类](https://github.com/Furinaaa-Cancan/mygo-mujica-archive/tree/main/assets/AveMujica/art/cards-wikia)取得；其[来源说明](https://github.com/Furinaaa-Cancan/mygo-mujica-archive/blob/main/assets/README.md)标明通过 Bandori Fandom API 整理。镜像目录名不代表图片中的人物已进入 Ave Mujica 时间线；本次使用的是 MyGO 时期的正常人物形象。
- 官方页面标注相关图片、照片和插画禁止未经授权复制或转载；本目录中的图片只用于 anivag 本地研究和模型能力验证，并已通过 `.gitignore` 排除。
