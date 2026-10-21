# UniXNU-erhai

OnePlus Pad 2 Pro (erhai / OPD2413, SM8750P) 的 UniXNU 设备仓库。

结构派生自 [IssacL04/UniXNU-Sun_SM8750](https://github.com/IssacL04/UniXNU-Sun_SM8750)
（一加 13T，同 SM8750 / Sun 平台），目标是把 XNU 送进入口（绿色条 + S1..S6）。

## 本机实测值

| 项 | 值 | 来源 |
| --- | --- | --- |
| FD base / size | `0xA7000000` / `0x2A8000` | 原厂 `uefi_a` 的 ELF `PT_LOAD` |
| splash_region | `0xFC800000` + `0x2B00000` | 真机 DT `/reserved-memory/splash_region` |
| GIC | `0x16000000` / `GICR 0x16080000` | 真机 DT `/soc/interrupt-controller@16000000` |
| GICR stride | `0x40000` | SM8750 标准值（真机 DT 未声明） |
| timebase | 19.2 MHz | `CNTFRQ_EL0` |
| 面板 | 3392×2400 `XN242_P_D_A0024_vid` | `/proc/cmdline` |
| 触控 | Novatek `nf_nt36536` | `/proc/cmdline` |
| UFS | `0x1d84000` | `/proc/iomem`（本 profile 未启用） |
| boot 分区 | 96 MiB，Android 头 v4 | `fastboot getvar all` |

## ★ 关键：`embed_payload`

```toml
[bootimg]
embed_payload = true
```

**一加的 ABL 在 `fastboot boot` 时仍会从 `init_boot` 取通用 ramdisk**，把传入的
ramdisk 字段整个替换掉。默认行为把 payload 放在 ramdisk 字段，因此会被换走，
UniXNU 找不到 `unixnu.toml`。

`embed_payload = true` 改成：payload 追加在 UEFI 固件之后一起 gzip 进 **kernel 字段**，
Android ramdisk 字段留空，由 BootShim → SEC → payload HOB → `BootPayloadDxe`
发布成只读 Block I/O，FAT 驱动再从中读配置。

## 目录

```
board.toml                    板级描述
configs/erhai_defconfig       源码内核配置（无 UFS/AES/GPU）
patches/unixnu-source-kernel.patch   上游适配补丁（含 embed_payload）
scripts/make-erhai-adt.py     Apple 设备树生成器（cpu0 + GICv3）
scripts/test-adt.py           ADT 契约测试
scripts/test-upstream.py      入口脚本 + 板级配置测试
scripts/check-memory-map.py   UEFI 内存表 vs 真机 DT 交叉校验
drivers/BootProgress/         S1..S6 屏幕检查点 + panic 页
.github/workflows/xnu.yml     macOS Actions 内核构建
upstream.rev                  锁定的上游提交
unixnu                        入口脚本（准备补丁树后转发）
```

## 本地使用（Windows / Linux，做 UEFI 与打包）

```sh
# 首次会克隆 upstream 并应用补丁，产物在 .cache/upstream-<rev>-<hash>
python unixnu --prepare

python unixnu defconfig erhai_defconfig
python unixnu bootimg --dtb <真机 DTB> --ramdisk <payload.img> -o boot.img
```

`unixnu` 会自动找 `../UEFI-erhai`；本机没有时退回
`E:\one plus\tools\Mu-Silicium`（erhaiPkg 所在检出）。

## 编内核（必须 macOS）

推到 GitHub 后：

```
Actions → erhai XNU (OnePlus Pad 2 Pro) → Run workflow → main
```

产物 `erhai-xnu-<run>` 内含 `kernel/kernel.macho`、`payload/payload.img`、
`payload/payload.json`、`logs/`、`unixnu.lock`。

## 真机测试

```bat
adb reboot bootloader
fastboot boot boot.img
```

**只用 `fastboot boot`，不要 `flash`。**

预期屏幕顺序：

```
绿色条                UEFI 已退出，DarwinEnter 跳板执行
S1: XNU entry, MMU off
S2: bootstrap maps ready
S3: MMU enabled
S4: arm_init entered
S5: PE_init_platform ready
S6: kernel VM and exception vectors ready
```

判定：

| 现象 | 含义 |
| --- | --- |
| USB 消失、不回 Android | 代码在 EL1 跑（正常） |
| 约 26 秒回 Android | 固件崩了被 fallback |
| 看门狗约 11 分钟后复位 | 死循环兜底，等它自己回来即可 |
| 屏幕停在某个 S 标记 | 卡在那一步 |

## 硬复位

**电源键 + 音量上 长按 15 秒**。

## 上游同步

`upstream.rev` 固定构建所用的上游提交。升级时先改这个文件、检查补丁与测试，
确认兼容再提交，避免直接改变可用基线。
