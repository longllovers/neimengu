#!/bin/bash

set -u

CHECK_INTERVAL=2
MOUNT_POINT="/mnt/usb_disk"
# 脚本由 sudo 启动时，SUDO_UID/GID 是启动网页服务的原始用户。
# 非 sudo 直接执行时，再回退到当前用户。
MOUNT_UID="${SUDO_UID:-$(id -u)}"
MOUNT_GID="${SUDO_GID:-$(id -g)}"

echo "正在记录当前硬盘，请暂时不要插入新硬盘……"

# 只记录磁盘，不记录分区
mapfile -t BEFORE_DISKS < <(
    lsblk -dnro NAME,TYPE |
    awk '$2 == "disk" {print $1}' |
    sort
)

echo "记录完成，请现在插入新硬盘……"

while true; do
    mapfile -t AFTER_DISKS < <(
        lsblk -dnro NAME,TYPE |
        awk '$2 == "disk" {print $1}' |
        sort
    )

    NEW_DISK=$(
        comm -13 \
            <(printf '%s\n' "${BEFORE_DISKS[@]}") \
            <(printf '%s\n' "${AFTER_DISKS[@]}") |
        head -n 1
    )

    if [ -n "$NEW_DISK" ]; then
        DISK="/dev/$NEW_DISK"
        echo "检测到新插入硬盘：$DISK"
        break
    fi

    sleep "$CHECK_INTERVAL"
done

echo "等待系统识别分区……"
sleep 10

echo "新硬盘信息："
lsblk -f "$DISK"

# 从新硬盘中找带文件系统的分区
mapfile -t PARTITIONS < <(
    lsblk -lnpo NAME,TYPE,FSTYPE "$DISK" |
    awk '$2 == "part" && $3 != "" {print $1}'
)

if [ ${#PARTITIONS[@]} -eq 0 ]; then
    echo "错误：没有找到可挂载的数据分区。"
    exit 1
fi

# 如果有多个数据分区，默认选择容量最大的一个
PARTITION=$(
    for DEVICE in "${PARTITIONS[@]}"; do
        SIZE=$(lsblk -bdno SIZE "$DEVICE")
        echo "$SIZE $DEVICE"
    done |
    sort -nr |
    head -n 1 |
    awk '{print $2}'
)

FSTYPE=$(lsblk -ndo FSTYPE "$PARTITION")
LABEL=$(lsblk -ndo LABEL "$PARTITION")

echo
echo "选择的数据分区：$PARTITION"
echo "文件系统：$FSTYPE"
echo "卷标：${LABEL:-无}"

# 检查是否已经被系统自动挂载
CURRENT_MOUNT=$(findmnt -rn -S "$PARTITION" -o TARGET | head -n 1)

if [ -n "$CURRENT_MOUNT" ]; then
    echo "该分区已经挂载在：$CURRENT_MOUNT"
    echo "正在卸载……"

    if ! sudo umount "$PARTITION"; then
        echo "错误：卸载失败，可能有程序正在使用该硬盘。"
        echo "可以执行以下命令查看占用："
        echo "sudo fuser -vm \"$CURRENT_MOUNT\""
        exit 1
    fi

    echo "卸载成功。"
fi

# 防止目标挂载目录已经挂载其他设备
if findmnt -rn -T "$MOUNT_POINT" >/dev/null 2>&1; then
    EXISTING_SOURCE=$(findmnt -rn -T "$MOUNT_POINT" -o SOURCE)
    EXISTING_TARGET=$(findmnt -rn -T "$MOUNT_POINT" -o TARGET)

    if [ "$EXISTING_TARGET" = "$MOUNT_POINT" ]; then
        echo "错误：目标目录已经挂载了其他设备：$EXISTING_SOURCE"
        echo "挂载位置：$MOUNT_POINT"
        exit 1
    fi
fi

sudo mkdir -p "$MOUNT_POINT"

# 根据文件系统设置挂载参数
case "$FSTYPE" in
    ntfs|ntfs3|exfat|vfat)
        MOUNT_OPTIONS="uid=$MOUNT_UID,gid=$MOUNT_GID,umask=0022"
        ;;
    *)
        MOUNT_OPTIONS="defaults"
        ;;
esac

echo "正在挂载到：$MOUNT_POINT"

if ! sudo mount -o "$MOUNT_OPTIONS" "$PARTITION" "$MOUNT_POINT"; then
    echo "错误：挂载失败。"
    exit 1
fi

# ext4 等 Linux 原生文件系统不支持 uid/gid 挂载参数。
# 将新磁盘根目录交给启动网页服务的用户，允许其创建输出子文件夹。
case "$FSTYPE" in
    ntfs|ntfs3|exfat|vfat)
        ;;
    *)
        if ! sudo chown "$MOUNT_UID:$MOUNT_GID" "$MOUNT_POINT"; then
            echo "错误：无法设置挂载目录权限。"
            exit 1
        fi
        ;;
esac

echo "挂载目录用户：uid=$MOUNT_UID gid=$MOUNT_GID"

FINAL_MOUNT=$(findmnt -rn -S "$PARTITION" -o TARGET | head -n 1)

echo
echo "挂载成功！"
echo "新硬盘：$DISK"
echo "数据分区：$PARTITION"
echo "挂载路径：$FINAL_MOUNT"
echo
df -h "$FINAL_MOUNT"
