yum install -y epel-release libvirt libvirt-devel pkgconfig gcc
yum install -y python-pip
pip install --upgrade pip
pip install https://github.com/amoskong/avocado-ec2.git

# avocado-ec2 is used for ami-test
pip install git+https://github.com/amoskong/avocado-ec2.git@branch-scylladb
