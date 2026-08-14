#!/usr/bin/env bash

# 将 country_data/市/旗县/ 下的数据合并到根目录的 六位代码_旗县/，
# 将 city_data/市/ 下的数据合并到根目录的 市影像/市/，
# 将 sentinel_data/市/ 下的数据合并到根目录的 哨兵原始影像/市/。
#
# 用法：
#   bash move_data.sh --dry-run   # 仅预览，不修改文件
#   bash move_data.sh --overwrite # 覆盖目标中的同名项
#   bash move_data.sh             # 正式移动
#   bash move_data.sh --country-dir 县数据 --city-dir 市数据 --sentinel-dir 哨兵数据
#   bash move_data.sh 县数据 市数据 哨兵数据

set -uo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
CITY_IMAGE_DIR="$ROOT_DIR/市影像"
SENTINEL_IMAGE_DIR="$ROOT_DIR/哨兵原始影像"

DRY_RUN=0
OVERWRITE=0
COUNTRY_INPUT="country_data"
CITY_INPUT="city_data"
SENTINEL_INPUT="sentinel_data"
POSITIONAL_COUNT=0

show_usage() {
  cat <<EOF
用法:
  $0 [选项]
  $0 [旗县数据目录] [市级数据目录] [哨兵数据目录]

选项:
  --country-dir DIR   指定旗县数据目录（默认: country_data）
  --city-dir DIR      指定市级数据目录（默认: city_data）
  --sentinel-dir DIR  指定哨兵数据目录（默认: sentinel_data）
  --dry-run, -n       仅预览，不修改文件
  --overwrite         覆盖目标中的同名项（默认不覆盖）
  --help, -h          显示帮助

相对目录以脚本所在目录为基准，也可以传入绝对路径。
EOF
}

while (( $# > 0 )); do
  case "$1" in
    --country-dir)
      if (( $# < 2 )); then
        printf '错误：--country-dir 后面必须提供目录。\n' >&2
        show_usage >&2
        exit 2
      fi
      COUNTRY_INPUT=$2
      shift 2
      ;;
    --country-dir=*)
      COUNTRY_INPUT=${1#*=}
      shift
      ;;
    --city-dir)
      if (( $# < 2 )); then
        printf '错误：--city-dir 后面必须提供目录。\n' >&2
        show_usage >&2
        exit 2
      fi
      CITY_INPUT=$2
      shift 2
      ;;
    --city-dir=*)
      CITY_INPUT=${1#*=}
      shift
      ;;
    --sentinel-dir)
      if (( $# < 2 )); then
        printf '错误：--sentinel-dir 后面必须提供目录。\n' >&2
        show_usage >&2
        exit 2
      fi
      SENTINEL_INPUT=$2
      shift 2
      ;;
    --sentinel-dir=*)
      SENTINEL_INPUT=${1#*=}
      shift
      ;;
    --dry-run|-n)
      DRY_RUN=1
      shift
      ;;
    --overwrite)
      OVERWRITE=1
      shift
      ;;
    --help|-h)
      show_usage
      exit 0
      ;;
    --)
      shift
      while (( $# > 0 )); do
        if (( POSITIONAL_COUNT == 0 )); then
          COUNTRY_INPUT=$1
        elif (( POSITIONAL_COUNT == 1 )); then
          CITY_INPUT=$1
        elif (( POSITIONAL_COUNT == 2 )); then
          SENTINEL_INPUT=$1
        else
          printf '错误：位置参数不能超过三个。\n' >&2
          show_usage >&2
          exit 2
        fi
        ((POSITIONAL_COUNT++))
        shift
      done
      ;;
    -*)
      printf '错误：未知选项: %s\n' "$1" >&2
      show_usage >&2
      exit 2
      ;;
    *)
      if (( POSITIONAL_COUNT == 0 )); then
        COUNTRY_INPUT=$1
      elif (( POSITIONAL_COUNT == 1 )); then
        CITY_INPUT=$1
      elif (( POSITIONAL_COUNT == 2 )); then
        SENTINEL_INPUT=$1
      else
        printf '错误：位置参数不能超过三个。\n' >&2
        show_usage >&2
        exit 2
      fi
      ((POSITIONAL_COUNT++))
      shift
      ;;
  esac
done

if [[ -z "$COUNTRY_INPUT" || -z "$CITY_INPUT" || -z "$SENTINEL_INPUT" ]]; then
  printf '错误：数据目录不能为空。\n' >&2
  exit 2
fi

if [[ "$COUNTRY_INPUT" = /* ]]; then
  COUNTRY_DIR=$COUNTRY_INPUT
else
  COUNTRY_DIR="$ROOT_DIR/$COUNTRY_INPUT"
fi

if [[ "$CITY_INPUT" = /* ]]; then
  CITY_DIR=$CITY_INPUT
else
  CITY_DIR="$ROOT_DIR/$CITY_INPUT"
fi

if [[ "$SENTINEL_INPUT" = /* ]]; then
  SENTINEL_DIR=$SENTINEL_INPUT
else
  SENTINEL_DIR="$ROOT_DIR/$SENTINEL_INPUT"
fi

if (( BASH_VERSINFO[0] < 4 )); then
  printf '错误：此脚本需要 Bash 4.0 或更高版本。\n' >&2
  exit 1
fi

declare -A COUNTY_CODE=(
  ["新城区"]="150102"
  ["回民区"]="150103"
  ["玉泉区"]="150104"
  ["赛罕区"]="150105"
  ["土默特左旗"]="150121"
  ["托克托县"]="150122"
  ["和林格尔县"]="150123"
  ["清水河县"]="150124"
  ["武川县"]="150125"
  ["东河区"]="150202"
  ["昆都仑区"]="150203"
  ["青山区"]="150204"
  ["石拐区"]="150205"
  ["白云鄂博矿区"]="150206"
  ["九原区"]="150207"
  ["土默特右旗"]="150221"
  ["固阳县"]="150222"
  ["达尔罕茂明安联合旗"]="150223"
  ["海勃湾区"]="150302"
  ["海南区"]="150303"
  ["乌达区"]="150304"
  ["红山区"]="150402"
  ["元宝山区"]="150403"
  ["松山区"]="150404"
  ["阿鲁科尔沁旗"]="150421"
  ["巴林左旗"]="150422"
  ["巴林右旗"]="150423"
  ["林西县"]="150424"
  ["克什克腾旗"]="150425"
  ["翁牛特旗"]="150426"
  ["喀喇沁旗"]="150428"
  ["宁城县"]="150429"
  ["敖汉旗"]="150430"
  ["科尔沁区"]="150502"
  ["科尔沁左翼中旗"]="150521"
  ["科尔沁左翼后旗"]="150522"
  ["开鲁县"]="150523"
  ["库伦旗"]="150524"
  ["奈曼旗"]="150525"
  ["扎鲁特旗"]="150526"
  ["霍林郭勒市"]="150581"
  ["东胜区"]="150602"
  ["康巴什区"]="150603"
  ["达拉特旗"]="150621"
  ["准格尔旗"]="150622"
  ["鄂托克前旗"]="150623"
  ["鄂托克旗"]="150624"
  ["杭锦旗"]="150625"
  ["乌审旗"]="150626"
  ["伊金霍洛旗"]="150627"
  ["海拉尔区"]="150702"
  ["扎赉诺尔区"]="150703"
  ["阿荣旗"]="150721"
  ["莫力达瓦达斡尔族自治旗"]="150722"
  ["鄂伦春自治旗"]="150723"
  ["鄂温克族自治旗"]="150724"
  ["陈巴尔虎旗"]="150725"
  ["新巴尔虎左旗"]="150726"
  ["新巴尔虎右旗"]="150727"
  ["满洲里市"]="150781"
  ["牙克石市"]="150782"
  ["扎兰屯市"]="150783"
  ["额尔古纳市"]="150784"
  ["根河市"]="150785"
  ["临河区"]="150802"
  ["五原县"]="150821"
  ["磴口县"]="150822"
  ["乌拉特前旗"]="150823"
  ["乌拉特中旗"]="150824"
  ["乌拉特后旗"]="150825"
  ["杭锦后旗"]="150826"
  ["集宁区"]="150902"
  ["卓资县"]="150921"
  ["化德县"]="150922"
  ["商都县"]="150923"
  ["兴和县"]="150924"
  ["凉城县"]="150925"
  ["察哈尔右翼前旗"]="150926"
  ["察哈尔右翼中旗"]="150927"
  ["察哈尔右翼后旗"]="150928"
  ["四子王旗"]="150929"
  ["丰镇市"]="150981"
  ["乌兰浩特市"]="152201"
  ["阿尔山市"]="152202"
  ["科尔沁右翼前旗"]="152221"
  ["科尔沁右翼中旗"]="152222"
  ["扎赉特旗"]="152223"
  ["突泉县"]="152224"
  ["二连浩特市"]="152501"
  ["锡林浩特市"]="152502"
  ["阿巴嘎旗"]="152522"
  ["苏尼特左旗"]="152523"
  ["苏尼特右旗"]="152524"
  ["东乌珠穆沁旗"]="152525"
  ["西乌珠穆沁旗"]="152526"
  ["太仆寺旗"]="152527"
  ["镶黄旗"]="152528"
  ["正镶白旗"]="152529"
  ["正蓝旗"]="152530"
  ["多伦县"]="152531"
  ["阿拉善左旗"]="152921"
  ["阿拉善右旗"]="152922"
  ["额济纳旗"]="152923"
)

moved_count=0
conflict_count=0
error_count=0
unknown_count=0

make_dir() {
  local dir=$1
  if (( DRY_RUN )); then
    [[ -d "$dir" ]] || printf '[预演] 创建目录: %s\n' "$dir"
  elif ! mkdir -p -- "$dir"; then
    printf '错误：无法创建目录: %s\n' "$dir" >&2
    ((error_count++))
    return 1
  fi
}

# 覆盖目标中的同名项并移动源项。
overwrite_item() {
  local src=$1
  local target=$2

  if (( DRY_RUN )); then
    printf '[预演] 覆盖并移动: %s -> %s\n' "$src" "$target"
    ((moved_count++))
  elif ! rm -rf -- "$target"; then
    printf '错误：删除同名目标失败: %s\n' "$target" >&2
    ((error_count++))
  elif mv -- "$src" "$target"; then
    printf '已覆盖并移动: %s -> %s\n' "$src" "$target"
    ((moved_count++))
  else
    printf '错误：覆盖移动失败: %s -> %s\n' "$src" "$target" >&2
    ((error_count++))
  fi
}

# 递归合并目录。已有的同名目录会继续合并；同名项仅在指定 --overwrite 时覆盖。
merge_dir() {
  local src=$1
  local dest=$2
  local item name target

  make_dir "$dest" || return 1

  while IFS= read -r -d '' item; do
    name=${item##*/}
    target="$dest/$name"

    if [[ -d "$item" && ! -L "$item" ]]; then
      if [[ -e "$target" || -L "$target" ]]; then
        if [[ -d "$target" && ! -L "$target" ]]; then
          merge_dir "$item" "$target"
        elif (( OVERWRITE )); then
          overwrite_item "$item" "$target"
        else
          printf '冲突（目标不是目录，已保留源数据）: %s -> %s\n' "$item" "$target" >&2
          ((conflict_count++))
        fi
      elif (( DRY_RUN )); then
        printf '[预演] 移动目录: %s -> %s\n' "$item" "$target"
        # 目录可整体移动，无需逐个打印内部文件。
        ((moved_count++))
      elif mv -- "$item" "$dest/"; then
        printf '已移动目录: %s -> %s\n' "$item" "$target"
        ((moved_count++))
      else
        printf '错误：移动目录失败: %s -> %s\n' "$item" "$target" >&2
        ((error_count++))
      fi
    elif [[ -e "$target" || -L "$target" ]]; then
      if (( OVERWRITE )); then
        overwrite_item "$item" "$target"
      else
        printf '冲突（同名项已存在，已保留源数据）: %s -> %s\n' "$item" "$target" >&2
        ((conflict_count++))
      fi
    elif (( DRY_RUN )); then
      printf '[预演] 移动: %s -> %s\n' "$item" "$target"
      ((moved_count++))
    elif mv -- "$item" "$dest/"; then
      printf '已移动: %s -> %s\n' "$item" "$target"
      ((moved_count++))
    else
      printf '错误：移动失败: %s -> %s\n' "$item" "$target" >&2
      ((error_count++))
    fi
  done < <(find "$src" -mindepth 1 -maxdepth 1 -print0)

  # 只删除已经搬空的来源目录；有冲突时目录会被保留。
  if (( ! DRY_RUN )); then
    rmdir -- "$src" 2>/dev/null || true
  fi
}

move_country_data() {
  local city_dir county_dir county_name code dest

  if [[ ! -d "$COUNTRY_DIR" ]]; then
    printf '提示：未找到，跳过: %s\n' "$COUNTRY_DIR"
    return
  fi

  printf '\n=== 处理旗县数据 ===\n'
  while IFS= read -r -d '' city_dir; do
    while IFS= read -r -d '' county_dir; do
      county_name=${county_dir##*/}
      code=${COUNTY_CODE[$county_name]:-}
      if [[ -z "$code" ]]; then
        printf '未知旗县（字典中无代码，已跳过）: %s\n' "$county_dir" >&2
        ((unknown_count++))
        continue
      fi

      dest="$ROOT_DIR/${code}_${county_name}"
      printf '\n旗县: %s -> %s\n' "$county_dir" "$dest"
      merge_dir "$county_dir" "$dest"
    done < <(find "$city_dir" -mindepth 1 -maxdepth 1 -type d -print0)

    if (( ! DRY_RUN )); then
      rmdir -- "$city_dir" 2>/dev/null || true
    fi
  done < <(find "$COUNTRY_DIR" -mindepth 1 -maxdepth 1 -type d -print0)

  if (( ! DRY_RUN )); then
    if rmdir -- "$COUNTRY_DIR" 2>/dev/null; then
      printf '已移除空来源目录: %s\n' "$COUNTRY_DIR"
    else
      printf '来源目录非空，已保留: %s\n' "$COUNTRY_DIR" >&2
    fi
  fi
}

move_city_data() {
  local city_dir city_name dest

  if [[ ! -d "$CITY_DIR" ]]; then
    printf '提示：未找到，跳过: %s\n' "$CITY_DIR"
    return
  fi

  printf '\n=== 处理市级影像 ===\n'
  make_dir "$CITY_IMAGE_DIR" || return

  while IFS= read -r -d '' city_dir; do
    city_name=${city_dir##*/}
    dest="$CITY_IMAGE_DIR/$city_name"
    printf '\n城市: %s -> %s\n' "$city_dir" "$dest"
    merge_dir "$city_dir" "$dest"
  done < <(find "$CITY_DIR" -mindepth 1 -maxdepth 1 -type d -print0)

  if (( ! DRY_RUN )); then
    if rmdir -- "$CITY_DIR" 2>/dev/null; then
      printf '已移除空来源目录: %s\n' "$CITY_DIR"
    else
      printf '来源目录非空，已保留: %s\n' "$CITY_DIR" >&2
    fi
  fi
}

move_sentinel_data() {
  local city_dir city_name dest

  if [[ ! -d "$SENTINEL_DIR" ]]; then
    printf '提示：未找到，跳过: %s\n' "$SENTINEL_DIR"
    return
  fi

  printf '\n=== 处理哨兵原始影像 ===\n'
  make_dir "$SENTINEL_IMAGE_DIR" || return

  while IFS= read -r -d '' city_dir; do
    city_name=${city_dir##*/}
    dest="$SENTINEL_IMAGE_DIR/$city_name"
    printf '\n城市: %s -> %s\n' "$city_dir" "$dest"
    merge_dir "$city_dir" "$dest"
  done < <(find "$SENTINEL_DIR" -mindepth 1 -maxdepth 1 -type d -print0)

  if (( ! DRY_RUN )); then
    if rmdir -- "$SENTINEL_DIR" 2>/dev/null; then
      printf '已移除空来源目录: %s\n' "$SENTINEL_DIR"
    else
      printf '来源目录非空，已保留: %s\n' "$SENTINEL_DIR" >&2
    fi
  fi
}

printf '脚本根目录: %s\n' "$ROOT_DIR"
printf '旗县数据目录: %s\n' "$COUNTRY_DIR"
printf '市级数据目录: %s\n' "$CITY_DIR"
printf '哨兵数据目录: %s\n' "$SENTINEL_DIR"
if (( DRY_RUN )); then
  printf '当前模式: 预演（不会修改任何文件）\n'
else
  printf '当前模式: 正式移动\n'
fi
if (( OVERWRITE )); then
  printf '同名项处理: 覆盖\n'
fi

move_country_data
move_city_data
move_sentinel_data

printf '\n=== 完成 ===\n'
printf '成功移动: %d 项；同名冲突: %d 项；未知旗县: %d 个；错误: %d 个\n' \
  "$moved_count" "$conflict_count" "$unknown_count" "$error_count"

if (( conflict_count > 0 || unknown_count > 0 || error_count > 0 )); then
  printf '注意：存在未移动的数据，请检查上面的提示。\n' >&2
  exit 1
fi
