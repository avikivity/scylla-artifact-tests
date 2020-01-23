
WORKSPACE=`pwd`
WORK_DIR='/workdir'

docker run -v $WORKSPACE:/workdir \
    --privileged \
    -v /var/run:/run \
    -v /sys/fs/cgroup:/sys/fs/cgroup:ro \
    -v /tmp:/tmp \
    -v /var/tmp:/var/tmp \
    -v ${HOME}:${HOME} \
    -v /etc/passwd:/etc/passwd:ro \
    -v /etc/group:/etc/group:ro \
    -w ${WORK_DIR} \
    --network host\
    $@  # {image} {avocado cmdline}
