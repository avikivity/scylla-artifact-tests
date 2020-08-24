# Tested by Fedora 30

yum install -y libvirt libvirt-devel pkgconfig gcc git python-devel xz-devel
yum install -y openssl-devel gdb-gdbserver
yum install -y python2-pip
pip install --upgrade pip

# libvirt-python
yum install -y libvirt-python

pip install -r requirements-python.txt
pip install avocado-framework==36.4

pip install https://github.com/amoskong/avocado-ec2.git

# avocado-ec2 is used for ami-test
pip install git+https://github.com/amoskong/avocado-ec2.git@branch-scylladb
