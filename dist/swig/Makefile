PWD=$(shell pwd)

all: bin/swig

bin/swig: swig-3.0.8
	cd $< && wget ftp://ftp.csx.cam.ac.uk/pub/software/programming/pcre/pcre-8.39.tar.gz && Tools/pcre-build.sh && ./configure --prefix=$(PWD) && make && make install && make clean && make distclean

swig-3.0.8 : swig-3.0.8.tar.gz
	tar zxvf $<

swig-3.0.8.tar.gz :
	wget http://downloads.sourceforge.net/project/swig/swig/swig-3.0.8/$@ -O $@
