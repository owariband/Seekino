Objectives and Key Results
目标与关键成果法

# 9.23 ～ 9.30
## P0

### 1. parse算法重构、提供更加高效的算法
hypnos: 串行select->epoll策略，多轮矫正，可接
ps：
1. chunk length应该可以设置，1min～15s不等，于是第二阶段的多元组epoll再纠偏，应该调整成按照时间总长组合而非特定的count
2. chunk length可以设置也是为了A、B实验 + user自定义高级设定

### 2. 实时识别的sub-agent开发，接入chat supervisor



### 3. 修复parse，使得speaker能够准确归类为相应角色，用于蒸馏
hypnos: 根据understanding agent结合video理解来调整asr的speaker归因，asr先落库改成该turn chunk全量解析完后再整体落库

### 4. local的时候视频上传被copy了额外的一份，需要考虑是否合理——local的时候是否软link即可？


### 5. retrieve算法（非harness）策略优化
eg.BM25使用优化、精排设计处理
旧的三路召回：
一个 text vector，它的输入是：
self_compact
+ 每个 owned event 的 description
+ 每个 owned event 的 text

完整 Chunk 的vision vector，粒度太粗用了1min（全量video）的，不知道效果如何

存在的问题总结：
没有设置合理的粒度逐级展开式向量




## P1


### 1. 各类参数为user提供可配置化、调整前端让这个可配置化好用


### 2. 前端UI美化、提供给agent的数据干净化


### 3. 算法engine的时间依赖毫秒->秒


### 4. 优化视频剪辑skill


### 5. 支持解析台词导出功能用于skill蒸馏


### 6. 视频综合理解维度划分，增加文本向量化维度召回

