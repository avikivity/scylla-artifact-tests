
FROM centos:7

ADD install-prereqs.sh install-prereqs.sh
RUN ./install-prereqs.sh
ADD requirements-python.txt requirements-python.txt
RUN pip install setuptools==40.8.0
RUN grep -v '^#' requirements-python.txt | xargs -t -L 1 pip install
