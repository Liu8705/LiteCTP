# LiteCTP：Python 版 CTP 期货接口

LiteCTP 使用 SWIG 封装官方 CTP C++ API，提供 Python 行情、交易接口和回调支持。项目通过 GitHub Actions 云端编译生成可用 pip 安装的 wheel 文件。

项目地址：[Liu8705/LiteCTP](https://github.com/Liu8705/LiteCTP)

## 当前版本

| 项目 | 当前配置 |
|---|---|
| Python 发行包名称 | `LiteCTP` |
| Python 导入名称 | `LiteCTP`，区分大小写 |
| 默认 CTP API 版本 | `6.7.13` |
| 当前使用的 wheel 对应 Python | CPython `3.10`，64 位 |
| Windows 安装包 | `win_amd64`，适用于 Windows x64 |
| Linux 安装包 | `manylinux`、`x86_64`，面向 Debian 等使用 glibc 的 Linux 系统 |

当前云端工作流以 `manylinux_2_28` 为 Linux 构建目标，要求目标系统使用 glibc 2.28 或以上，并在 Debian Bookworm 容器中进行安装、导入验证。具体兼容标记以实际 wheel 文件名为准。

当前工作流构建 Windows 和 Linux x86_64 安装包；macOS、Linux ARM64 等平台未包含在该工作流中。其他 Python 版本需要选择对应版本重新构建并验证，不能直接使用 `cp310` 的安装包。

## 安装预编译 wheel

安装预编译 wheel 不需要在使用机器上安装 C++ 编译器或 SWIG。请先准备与安装包匹配的 Python 版本，建议使用独立虚拟环境。

当前通过 GitHub Actions 的 Artifacts 获取安装包，未通过本项目发布到 PyPI；安装时指定下载后的本地 `.whl` 文件。

### 下载安装包

1. 打开本仓库的 **Actions** 页面。
2. 选择 **Build LiteCTP Wheels**，打开所需构建记录。
3. 确认对应平台的构建和验证任务成功。
4. 在运行概览页面的 **Artifacts** 区域下载对应安装包。

Python 3.10 对应的 artifact 名称为：

| 平台 | Artifact 名称 |
|---|---|
| Windows x64 | `LiteCTP-windows-py3.10` |
| Linux x86_64 | `LiteCTP-linux-x86_64-py3.10` |

下载后解压外层 ZIP，得到 `.whl` 文件。保留 `.whl` 的完整原始名称，不要继续解压它，也不要删掉文件名中的版本和兼容标记。

### Windows

以下示例在已安装 Conda 的终端中执行；也可以使用已有的 CPython 3.10 虚拟环境。

```bat
conda create -n LiteCTP python=3.10 pip -y
conda activate LiteCTP
```

进入 Windows wheel 所在目录，确认文件后安装：

```bat
dir *.whl
python -m pip install .\litectp-6.7.13-cp310-cp310-win_amd64.whl
```

### Debian / Linux

先准备 CPython 3.10，再创建、激活虚拟环境：

```bash
python3.10 -m venv .venv
source .venv/bin/activate
```

进入 Linux wheel 所在目录，安装对应文件：

```bash
ls -lh *.whl
python -m pip install ./litectp-6.7.13-cp310-cp310-manylinux*.whl
```

上述通配符要求目录中只有一个匹配的 Linux 安装包；也可以直接填写 `ls` 显示的完整文件名。如果 `python3.10` 命令不存在，需要先准备该版本的 Python，或者为现有 Python 版本重新编译 wheel。

Windows wheel 不能安装到 Linux；Linux wheel 不能安装到 Windows。`cp310` 表示 CPython 3.10，不适用于 CPython 3.11 等其他版本。

### 验证安装

在安装所用的同一个虚拟环境中执行：

```bash
python -c "import LiteCTP, LiteCTP._ctp; from importlib.metadata import version; print('LiteCTP', version('LiteCTP')); print(LiteCTP.__file__)"
```

该检查验证包和底层扩展能否加载。连接登录、行情订阅、报单和成交回报等业务功能，需要结合实际期货公司的环境进一步验证。

### 更新本地安装

下载新构建的 wheel 后，在目标虚拟环境中执行：

```bash
python -m pip uninstall LiteCTP -y
```

然后按对应平台的安装步骤安装新文件。对于版本号相同的新构建，也可以使用：

```bash
python -m pip install --force-reinstall "新安装包的完整路径.whl"
```

请将示例中的路径替换为实际文件路径，并使用新编译的安装包。

## Python 中的使用方式

直接使用包名：

```python
import LiteCTP

field = LiteCTP.CThostFtdcReqUserLoginField()

class CMdSpiBase(LiteCTP.CThostFtdcMdSpi):
    def OnFrontConnected(self):
        print("行情前置已连接")
```

如已有代码通过 `ctp.类名`、`ctp.常量名` 使用接口，可以采用别名：

```python
import LiteCTP as ctp

field = ctp.CThostFtdcReqUserLoginField()

class CMdSpiBase(ctp.CThostFtdcMdSpi):
    def OnFrontConnected(self):
        print("行情前置已连接")
```

这样后续的 `ctp.` 用法可以保留。其他文件中的 `import ctp`、`from ctp import ...` 等导入语句，也需要改为对应的 LiteCTP 导入。

以上示例只演示对象创建和回调继承，不会建立连接或发送订单。

## libiconv 与字符串编码

封装层对匹配的 CTP 返回字符数组进行 GBK → UTF-8 转换，便于在 Python 中读取中文字段。

- **Windows 云端编译**：构建环境安装 `libiconv`，再由 `delvewheel` 收集所需 DLL 并修复 wheel 的加载路径。使用经过此流程打包、验证的 wheel 时，通常无需再单独执行 `conda install libiconv`。
- **Debian/Linux**：使用 glibc 提供的 `iconv` 接口，通常无需安装独立的 GNU libiconv。
- **自行源码编译**：仍需准备相应的头文件和链接库；Windows 构建环境仍需要 libiconv。

在 Windows 中可以查看已安装包是否携带 iconv 相关文件：

```bat
python -m pip show -f LiteCTP | findstr /i "iconv"
```

如果出现 DLL 加载错误，应核对 wheel 是否完成依赖修复、Python 版本是否匹配，以及所需 DLL 是否齐全。

## 使用 GitHub Actions 云端编译

1. 将源代码、SDK 和构建配置的修改提交到本仓库。
2. 打开 **Actions → Build LiteCTP Wheels → Run workflow**。
3. 选择构建分支，通常为 `master`。
4. 设置 `target_platform`：`windows`、`linux` 或 `both`。
5. 设置 `python_version`，当前使用 `3.10`。
6. 点击 **Run workflow**，启动一次新的构建。
7. 等待对应平台的构建、验证成功，再下载 Artifacts。

每次运行为所选 Python 版本构建安装包。修改代码后应启动新的运行，使构建使用新提交。

云端流程包括：

- Linux：在 manylinux 环境编译，使用 `auditwheel` 修复依赖，并在 Debian 容器中安装、导入验证。
- Windows：在 Conda 构建环境编译，使用 `delvewheel` 打包依赖 DLL，并在另一份 Python 环境中安装、导入验证。

构建在 GitHub 提供的运行环境中完成；本机负责编辑、提交和下载安装包。

## 项目中的主要文件

| 路径 | 用途 |
|---|---|
| `LiteCTP/__init__.py` | Python 包入口，将内部封装接口导入外层包 |
| `ctp.i` | SWIG 接口定义、回调支持、异常处理和类型转换规则 |
| `setup.py` | 包元数据、平台链接配置、扩展构建和 SDK 文件复制 |
| `pyproject.toml` | 声明 Python 构建后端及其依赖 |
| `api/6.7.13/linux/` | Linux CTP SDK 头文件和动态库 |
| `api/6.7.13/windows/` | Windows CTP SDK 头文件、链接库和 DLL |
| `.github/workflows/` | GitHub Actions 云端构建工作流 |

`ctp.i` 中保留：

```swig
%module(directors="1") ctp
```

这里的 `ctp` 是内部 SWIG 模块名。生成的 `ctp.py` 和编译后的 `_ctp` 扩展放在 `LiteCTP` 包内，用户通过 `import LiteCTP` 使用接口，无需单独导入内部模块。

## 自行在本地编译（可选）

本地编译需要 C++ 编译环境，会占用本机 CPU、内存和磁盘资源。按照以下要求准备环境：

| 平台 | 构建环境要求 |
|---|---|
| Windows | 匹配版本的 Python、MSVC C++ 构建工具、SWIG、libiconv、Windows CTP SDK |
| Linux | 匹配版本的 Python 及开发头文件、gcc/g++、SWIG、Linux CTP SDK |

Windows 可在 Conda 构建环境中安装 SWIG 和 libiconv：

```bat
conda install -c conda-forge swig libiconv
```

下载本仓库源码：

```bash
git clone https://github.com/Liu8705/LiteCTP.git
cd LiteCTP
```

构建 wheel：

```bash
python -m pip install build
python -m build --wheel
```

生成文件位于 `dist` 目录。构建后端根据 `pyproject.toml` 创建隔离构建环境并安装 setuptools；C++ 编译器、SWIG、CTP SDK 等仍需提前准备。

本地生成的原始 wheel 不一定适合直接复制到其他机器。Windows 还需检查并打包非系统 DLL，Linux 还需满足目标系统的 glibc 和动态库兼容要求。需要分发时，可使用本项目的云端构建流程完成依赖修复与验证。

## 独立维护和升级 CTP SDK

默认使用 CTP API `6.7.13`。以后可以直接基于官方新版 SDK 更新 LiteCTP，无需同步原项目。

1. 取得官方对应平台的新版 SDK。
2. 在 `api/新版本/linux/`、`api/新版本/windows/` 中放入同一 SDK 版本的头文件和库文件，保持构建脚本要求的目录布局。
3. 修改 `setup.py` 中的默认 `API_VER`。
4. 统一修改工作流中的 API 版本号和 SDK 路径。
5. 检查新旧头文件、更新说明和 `ctp.i` 中的转换规则。
6. 重新编译，验证导入、连接、行情、回调及实际使用的交易接口。

SWIG 会读取官方头文件生成普通方法、结构体和常量的封装。新增特殊参数类型、指针传递方式或字符串编码变化时，可能需要调整 `ctp.i`；仅修改版本号不能代替 SDK 更新和接口验证。

本地编译时可以通过环境变量选择已经放入仓库的 SDK 版本。例如：

Linux Bash：

```bash
export API_VER=6.7.13
```

Windows PowerShell：

```powershell
$env:API_VER = "6.7.13"
```

Windows CMD：

```bat
set API_VER=6.7.13
```

当前云端工作流固定配置了 SDK 版本；本机设置的环境变量不会自动传到 GitHub Actions，需要同时更新工作流配置。

## 回调对象的使用

当前封装会对匹配的回调指针参数创建副本，并将其内存交由 Python 管理。回调中的 CTP 结构体对象因此可以保留引用或放入队列，供回调返回后的代码使用。

行情和交易回调中应尽量快速更新状态或入队；耗时计算、磁盘写入等操作可交给其他处理线程。

## 连接认证与设备信息采集

交易前置地址、BrokerID、AppID、AuthCode，以及 API 版本和设备信息采集要求，应按期货公司提供的接入说明配置。

遇到认证、握手或 Linux 设备信息采集错误时，先核对 SDK 版本、环境、系统依赖及原始错误信息，再根据期货公司的要求处理。测评版和生产版 SDK 的选择以其接入要求为准。

## 项目来源

LiteCTP 起源于 [keli/ctp-python](https://github.com/keli/ctp-python)，后续代码、构建配置和 SDK 更新由本仓库维护。构建使用本仓库的源码和 SDK，使用 LiteCTP 时无需安装 `ctp-python`。

保留仓库原有许可证与版权声明；官方 CTP SDK 及随包附带的第三方库遵循各自的使用和分发条款。

## 参考资料

- [Python 虚拟环境与 pip 安装](https://packaging.python.org/en/latest/guides/installing-using-pip-and-virtual-environments/)
- [手动运行 GitHub Actions 工作流](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/manually-run-a-workflow)
- [manylinux 平台兼容说明](https://github.com/pypa/manylinux)
- [delvewheel：Windows wheel 的 DLL 依赖打包](https://github.com/adang1345/delvewheel)
- [Linux iconv 接口](https://man7.org/linux/man-pages/man3/iconv.3.html)
