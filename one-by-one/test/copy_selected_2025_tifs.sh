#!/usr/bin/env bash

set -uo pipefail

readonly SOURCE_ROOT="/media/cangling/nas_folder/原始影像/0.5m影像"

usage() {
    cat <<'EOF'
用法：
  bash copy_selected_2025_tifs.sh --output <保存目录> [--jobs <并发数>]

参数：
  --output, -o  必填，复制文件的保存目录
  --jobs, -j    可选，并发 rsync 数，默认 4
  --help, -h    显示帮助

示例：
  bash copy_selected_2025_tifs.sh --output /media/cangling/new_disk/xilingol
  bash copy_selected_2025_tifs.sh --output /media/cangling/new_disk/xilingol --jobs 6
EOF
}

OUTPUT_DIR=""
JOBS=4

while (($#)); do
    case "$1" in
        --output|-o)
            if (($# < 2)); then
                echo "错误：$1 后面必须指定保存目录。" >&2
                usage >&2
                exit 2
            fi
            OUTPUT_DIR=$2
            shift 2
            ;;
        --jobs|-j)
            if (($# < 2)); then
                echo "错误：$1 后面必须指定并发数。" >&2
                usage >&2
                exit 2
            fi
            JOBS=$2
            shift 2
            ;;
        --help|-h)
            usage
            exit 0
            ;;
        *)
            echo "错误：未知参数 $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

if [[ -z "$OUTPUT_DIR" ]]; then
    echo "错误：缺少必填参数 --output。" >&2
    usage >&2
    exit 2
fi

if [[ ! "$JOBS" =~ ^[1-9][0-9]*$ ]]; then
    echo "错误：--jobs 必须是大于 0 的整数。" >&2
    exit 2
fi

for command_name in find rsync stat sort awk stdbuf tr; do
    if ! command -v "$command_name" >/dev/null 2>&1; then
        echo "错误：找不到命令 $command_name，请先安装。" >&2
        exit 1
    fi
done

if [[ ! -d "$SOURCE_ROOT" ]]; then
    echo "错误：影像根目录不存在：$SOURCE_ROOT" >&2
    exit 1
fi

if ! mkdir -p -- "$OUTPUT_DIR"; then
    echo "错误：无法创建输出目录：$OUTPUT_DIR" >&2
    exit 1
fi

if [[ ! -d "$OUTPUT_DIR" || ! -w "$OUTPUT_DIR" ]]; then
    echo "错误：输出目录不存在或不可写：$OUTPUT_DIR" >&2
    exit 1
fi

mapfile -t PLANE_NAMES <<'EOF'
K49E001022
K49E001023
K49E001024
K49E002016
K49E002017
K49E002019
K49E002022
K49E002023
K49E002024
K49E003017
K49E003019
K49E004017
K49E004019
K49E005018
K49E005019
K49E005020
K49E007018
K49E007019
K49E007020
K49E007021
K49E007024
K49E008016
K49E008018
K49E008019
K49E008020
K49E008024
K49E009017
K49E009018
K49E009019
K49E010017
K49E010020
K49E010021
K49E010022
K49E011019
K49E011020
K49E011021
K49E011022
K49E011023
K49E011024
K49E012019
K49E012020
K49E012021
K49E012022
K49E012023
K49E012024
K49E013020
K49E013021
K49E013024
K50E001001
K50E001004
K50E001005
K50E001008
K50E001009
K50E001010
K50E001011
K50E001012
K50E001013
K50E002001
K50E002006
K50E002007
K50E002008
K50E002010
K50E002011
K50E002012
K50E002013
K50E003004
K50E003005
K50E003007
K50E003008
K50E003009
K50E003010
K50E003011
K50E003012
K50E004007
K50E004009
K50E004010
K50E004011
K50E004012
K50E005004
K50E005007
K50E005009
K50E005010
K50E006005
K50E006006
K50E006007
K50E006008
K50E006009
K50E006010
K50E006011
K50E007003
K50E007004
K50E007005
K50E007006
K50E007007
K50E007008
K50E007009
K50E007010
K50E007011
K50E008002
K50E008003
K50E008004
K50E008005
K50E008006
K50E008007
K50E008008
K50E008009
K50E008010
K50E008011
K50E009001
K50E009002
K50E009003
K50E009004
K50E009005
K50E009006
K50E009007
K50E009008
K50E009009
K50E009010
K50E009011
K50E009012
K50E010002
K50E010003
K50E010004
K50E010005
K50E010006
K50E010007
K50E010008
K50E010009
K50E010010
K50E010011
K50E010012
K50E011001
K50E011002
K50E011003
K50E011004
K50E011005
K50E011006
K50E011007
K50E011008
K50E011009
K50E011010
K50E011011
K50E011012
K50E012001
K50E012002
K50E012004
K50E012005
K50E012006
K50E012007
K50E012008
K50E012009
K50E012010
K50E012011
K50E012012
K50E013001
K50E013004
K50E013005
K50E013006
K50E013007
K50E013008
K50E013009
K50E013010
K50E013011
K50E013012
K50E014004
K50E014005
K50E014006
K50E014007
K50E014008
K50E014009
K50E015004
K50E015005
K50E015006
L49E019019
L49E020021
L49E021018
L49E022019
L49E024021
L49E024022
L49E024023
L49E024024
L50E008024
L50E009023
L50E009024
L50E010021
L50E010022
L50E010023
L50E010024
L50E011020
L50E011021
L50E011022
L50E011023
L50E011024
L50E012020
L50E012021
L50E012022
L50E012023
L50E012024
L50E013015
L50E013020
L50E013021
L50E013022
L50E013023
L50E013024
L50E014013
L50E014020
L50E014021
L50E014022
L50E014023
L50E014024
L50E015010
L50E015012
L50E015013
L50E015022
L50E015023
L50E016011
L50E016012
L50E016022
L50E017008
L50E017011
L50E017014
L50E017019
L50E017022
L50E018005
L50E018009
L50E018017
L50E018018
L50E018020
L50E018021
L50E018022
L50E019005
L50E019009
L50E019010
L50E019011
L50E019014
L50E019018
L50E019019
L50E019020
L50E020005
L50E020008
L50E020011
L50E020014
L50E020015
L50E020016
L50E020018
L50E020019
L50E020020
L50E020021
L50E021001
L50E021006
L50E021009
L50E021010
L50E021011
L50E021013
L50E021014
L50E021015
L50E021016
L50E021017
L50E021019
L50E021020
L50E022007
L50E022008
L50E022009
L50E022010
L50E022011
L50E022013
L50E022014
L50E022015
L50E022016
L50E022017
L50E022019
L50E022020
L50E023004
L50E023008
L50E023009
L50E023010
L50E023011
L50E023012
L50E023013
L50E023014
L50E023015
L50E023016
L50E023017
L50E023018
L50E024004
L50E024008
L50E024009
L50E024010
L50E024011
L50E024012
L50E024013
L50E024015
L50E024016
L50E024017
L51E008001
L51E009001
EOF

declare -A WANTED=()
declare -A FOUND_BY_NAME=()
declare -a FOUND_FILES=()
declare -a MISSING_NAMES=()
declare -a SKIPPED_EXISTING=()

for plane_name in "${PLANE_NAMES[@]}"; do
    WANTED["${plane_name}_2025.tif"]=1
done

echo "正在扫描：$SOURCE_ROOT"
echo "目标文件数：${#PLANE_NAMES[@]}"

# 只遍历根目录一次。同名文件出现多次时，保留 find 返回的第一个。
while IFS= read -r -d '' tif_path; do
    tif_basename=${tif_path##*/}
    if [[ -n "${WANTED[$tif_basename]+x}" &&
          -z "${FOUND_BY_NAME[$tif_basename]+x}" ]]; then
        FOUND_BY_NAME["$tif_basename"]=$tif_path
        if ((${#FOUND_BY_NAME[@]} == ${#PLANE_NAMES[@]})); then
            break
        fi
    fi
done < <(find "$SOURCE_ROOT" -type f -name '*_2025.tif' -print0)

# 按图幅号清单的顺序整理结果。
total_bytes=0
source_found_count=0
for plane_name in "${PLANE_NAMES[@]}"; do
    tif_basename="${plane_name}_2025.tif"
    if [[ -n "${FOUND_BY_NAME[$tif_basename]+x}" ]]; then
        ((source_found_count += 1))
        tif_path=${FOUND_BY_NAME[$tif_basename]}
        if [[ -e "$OUTPUT_DIR/$tif_basename" ]]; then
            SKIPPED_EXISTING+=("$tif_basename")
        else
            FOUND_FILES+=("$tif_path")
            file_bytes=$(stat -c '%s' -- "$tif_path")
            ((total_bytes += file_bytes))
        fi
    else
        MISSING_NAMES+=("$tif_basename")
    fi
done

total_gib=$(awk -v bytes="$total_bytes" 'BEGIN {printf "%.2f", bytes / 1024 / 1024 / 1024}')
total_gb=$(awk -v bytes="$total_bytes" 'BEGIN {printf "%.2f", bytes / 1000 / 1000 / 1000}')

echo
echo "扫描完成：源目录找到 ${source_found_count} 个，未找到 ${#MISSING_NAMES[@]} 个。"
echo "输出目录已存在并跳过：${#SKIPPED_EXISTING[@]} 个。"
echo "本次待复制：${#FOUND_FILES[@]} 个，共 ${total_gib} GiB（${total_gb} GB）。"
echo "输出目录：$OUTPUT_DIR"
echo "并发数：$JOBS"

if ((${#MISSING_NAMES[@]} > 0)); then
    missing_log="$OUTPUT_DIR/missing_2025_tifs.txt"
    printf '%s\n' "${MISSING_NAMES[@]}" | sort > "$missing_log"
    echo "未找到的文件名已写入：$missing_log"
fi

if ((${#FOUND_FILES[@]} == 0)); then
    if ((source_found_count == 0)); then
        echo "没有找到任何目标 TIF，不执行复制。" >&2
        exit 1
    fi
    echo "源目录中找到的目标文件均已存在于输出目录，无需复制。"
    exit 0
fi

copy_one() {
    local src=$1
    local progress_file=$2
    local line

    printf '%s\n' "等待 rsync 输出进度..." > "$progress_file"

    # 每个任务只把最新进度写入自己的状态文件，由主进程统一刷新固定行。
    stdbuf -o0 -e0 \
        rsync -rt --ignore-existing --partial --human-readable --info=progress2 \
        -- "$src" "$OUTPUT_DIR/" 2>&1 |
        stdbuf -o0 tr '\r' '\n' |
        while IFS= read -r line; do
            if [[ -n "${line//[[:space:]]/}" ]]; then
                printf '%s\n' "$line" > "$progress_file"
            fi
        done
    local status=${PIPESTATUS[0]}

    if ((status == 0)); then
        printf '%s\n' "完成" > "$progress_file"
    else
        printf '失败（rsync 退出码：%d）\n' "$status" > "$progress_file"
    fi
    return "$status"
}

echo
echo "开始复制。每行格式：[本次序号/总数] 文件名 | 百分比、速度、剩余时间"

PROGRESS_DIR=$(mktemp -d)
trap 'rm -rf -- "$PROGRESS_DIR"' EXIT INT TERM

declare -a ACTIVE_PIDS=()
declare -A PID_TO_FILE=()
declare -A PID_TO_NUMBER=()
declare -A PID_TO_PROGRESS=()
failed_count=0
completed_count=0
next_file_index=0
dashboard_drawn=0
interactive_dashboard=0

if [[ -t 1 ]]; then
    interactive_dashboard=1
fi

launch_next() {
    local src
    local number
    local progress_file
    local pid

    if ((next_file_index >= ${#FOUND_FILES[@]})); then
        return 1
    fi

    src=${FOUND_FILES[$next_file_index]}
    number=$((next_file_index + 1))
    progress_file="$PROGRESS_DIR/$number.progress"
    printf '%s\n' "准备开始..." > "$progress_file"

    copy_one "$src" "$progress_file" &
    pid=$!
    ACTIVE_PIDS+=("$pid")
    PID_TO_FILE["$pid"]=$src
    PID_TO_NUMBER["$pid"]=$number
    PID_TO_PROGRESS["$pid"]=$progress_file
    ((next_file_index += 1))
}

render_dashboard() {
    local pid
    local src
    local file_name
    local number
    local progress
    local slot=0

    if ((interactive_dashboard == 0)); then
        return
    fi

    if ((dashboard_drawn == 1)); then
        printf '\033[%dA' "$JOBS"
    fi

    for pid in "${ACTIVE_PIDS[@]}"; do
        src=${PID_TO_FILE[$pid]}
        file_name=${src##*/}
        number=${PID_TO_NUMBER[$pid]}
        progress=$(<"${PID_TO_PROGRESS[$pid]}")
        # 清除整行后重画，确保较短的新内容不会残留旧字符。
        printf '\033[2K\r[%d/%d] %s | %s\n' \
            "$number" "${#FOUND_FILES[@]}" "$file_name" "$progress"
        ((slot += 1))
    done

    # 始终保持 JOBS 行，任务交接时光标不会跳动。
    while ((slot < JOBS)); do
        printf '\033[2K\r\n'
        ((slot += 1))
    done
    dashboard_drawn=1
}

clear_dashboard() {
    local slot
    if ((interactive_dashboard == 0 || dashboard_drawn == 0)); then
        return
    fi

    printf '\033[%dA' "$JOBS"
    for ((slot = 0; slot < JOBS; slot++)); do
        printf '\033[2K\r\n'
    done
}

# 先填满并发槽位。
while ((${#ACTIVE_PIDS[@]} < JOBS)); do
    launch_next || break
done

while ((${#ACTIVE_PIDS[@]} > 0)); do
    render_dashboard

    declare -a still_running=()
    for pid in "${ACTIVE_PIDS[@]}"; do
        if kill -0 "$pid" 2>/dev/null; then
            still_running+=("$pid")
            continue
        fi

        if wait "$pid"; then
            :
        else
            ((failed_count += 1))
        fi
        ((completed_count += 1))

        if ((interactive_dashboard == 0)); then
            src=${PID_TO_FILE[$pid]}
            number=${PID_TO_NUMBER[$pid]}
            progress=$(<"${PID_TO_PROGRESS[$pid]}")
            printf '[%d/%d] %s | %s\n' \
                "$number" "${#FOUND_FILES[@]}" "${src##*/}" "$progress"
        fi

        unset 'PID_TO_FILE[$pid]'
        unset 'PID_TO_NUMBER[$pid]'
        unset 'PID_TO_PROGRESS[$pid]'
    done
    ACTIVE_PIDS=("${still_running[@]}")

    while ((${#ACTIVE_PIDS[@]} < JOBS)); do
        launch_next || break
    done

    if ((${#ACTIVE_PIDS[@]} > 0)); then
        sleep 0.5
    fi
done

clear_dashboard

echo
if ((failed_count == 0)); then
    echo "复制完成：成功 ${#FOUND_FILES[@]} 个，失败 0 个，总容量 ${total_gib} GiB。"
    exit 0
else
    echo "复制结束：成功 $((${#FOUND_FILES[@]} - failed_count)) 个，失败 ${failed_count} 个。" >&2
    exit 1
fi
