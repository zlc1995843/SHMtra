# SHMtra

《Sweet Home Maid》离线启动器使用的简体中文剧情文件。

翻译文件保持游戏原始路径：

`assets/AdvStory/import/<UUID 前两位>/<UUID>.<版本指纹>.json`

启动器开启“中文模式”后，会获取一次仓库文件索引并比较 Git 指纹；仅在本地缺失或指纹变化时下载中文文件。仓库没有对应文件时，游戏自动回退到本地日文原版。

## 当前测试版

角色 108 共 123 个剧情文件、7,551 段文本；当前已有 3,672 段通过词库和保护标记校验。尚未完成的 3,879 段暂时保留日文，方便先测试启动器读取、称呼和剧情加载。

## 更新指定角色剧情

翻译工具不会把密钥写入文件，只读取进程环境中的 `DEEPSEEK_API_KEY`：

```powershell
python tools/translate_character.py --character 108 --model deepseek-v4-pro
```

工具会完成以下流程：

1. 读取游戏根目录最新的 `assets/AdvStory/config.*.json`。
2. 按角色编号筛选卡片剧情，并由 Cocos UUID 与版本指纹计算 CDN 路径。
3. 下载并校验全部剧情 TextAsset；403/404 不重试，其余网络错误最多重试 10 次。
4. 只抽取玩家能看到的对白和旁白；所有 `@` 命令、`$$` 资源引用及注释保持原样。
5. 使用 `deepseek-v4-pro` 翻译简体中文，并在 `.work` 中保存可续跑缓存。
6. 复核 JSON、剧情条目数、命令序列、资源路径和残留日文假名，再生成翻译清单。

只下载和统计、不调用翻译 API：

```powershell
python tools/translate_character.py --character 108 --download-only
```

只导出当前已校验的缓存，未完成部分保留日文且不调用翻译 API：

```powershell
python tools/translate_character.py --character 108 --model deepseek-v4-pro --export-partial
```
