Objectives and Key Results
目标与关键成果法

# 9.23 ～ 9.30
## P0

### 1. parse算法重构、提供更加高效的算法
hypnos: 串行select->epoll策略，多轮矫正，可接


### 2. 实时识别的sub-agent开发，接入chat supervisor



### 3. 修复parse，使得speaker能够准确归类为相应角色，用于蒸馏
hypnos: 根据understanding agent结合video理解来调整asr的speaker归因，asr先落库改成该turn chunk全量解析完后再整体落库

### 4. local的时候视频上传被copy了额外的一份，需要考虑是否合理——local的时候是否软link即可？


## P1


### 1. 各类参数为user提供可配置化、调整前端让这个可配置化好用


### 2. 前端UI美化、提供给agent的数据干净化


### 3. 算法engine的时间依赖毫秒->秒


### 4. 优化视频剪辑skill


### 5. 支持解析台词导出功能用于skill蒸馏


### 6. 视频综合理解维度划分，增加文本向量化维度召回

