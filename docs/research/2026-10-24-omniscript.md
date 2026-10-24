> 10.24 update
>


Background：
Seekino产品功能需求：

为了借鉴业界已有成熟的论文研究，复现为我们工程体系的一部分，需要对Paper[Omniscript](https://arxiv.org/html/2604.11102v1)建立体系的认识，
从V2S、数据层级的划分、Schema的定义、以及如何使用VLM等MultiModal实现等方面展开理解


# 论文理解


## 1. Video-to-Script Introduction

在过去已经有的概念：Caption、Scene Detection的前提下，V2S主要是用于解决什么问题？可以提供什么样的信息？



我们能从什么层次和结构，理解V2S？


数据层级上可以怎么看？解析逻辑上可以怎么看？


## 2. V2S的解析实现


实际上根据论文，他们这一块儿是闭源的，但是逻辑很简单。。
就是两遍串行的VLM识别，第一层先维护CPM（Character Profile Manager）负责快速维护人物身份、别称问题
第二层做script的schema识别输出

首先是通过SceneDetect识别scene的切分区间，然后他们为了训练数据才对scene进行识别。。。。所以他们的重心其实在于收集数据预训练他们的omniscript base model


## 3. 我们能参考的内容

1. CPM的维护，减少user的自行导入背景成本（可以先不管，反正都是cloud体验）

如下表
| OmniScript 内容           | 对我们的价值                          |
| ----------------------- | ------------------------------- |
| PySceneDetect           | **可删除**，除非实验证明切分有收益             |
| DeepSeek Thinking 蒸馏    | **可删除**，这是训练数据构造，不是在线解析必需步骤     |
| 自研 Base Model 和训练过程     | **不采用**                         |
| Meta／Scene／Event Schema | **直接参考**，尤其字段解耦、并发 Event、可选字段规则 |
| `high_points`           | **直接参考**，适合高光检索、剪辑和 HTML PPT    |
| CPM                     | **按需采用**，解决无预导入角色库、跨段身份和延迟命名    |
| 先剧情后细字段                 | **可作为二次 Refine**，不必固定成两次调用      |
| TSG                     | **作为全片解析失败后的降级方案**，不是默认架构       |
| 分层评测                    | **很有价值**，分别评价角色、字段语义和时间定位       |


## 4. 总结
全都是闭源的，也就schema能参考一下了。抠门