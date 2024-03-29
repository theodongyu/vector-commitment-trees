# Vector Commitment Trees

This repo was developed as part of my Bachelor Thesis at University of Copenhagen in Computer Science.

The main implementations are found in `\src` as `vbst.py`, `vb_tree.py` and `vbplus_tree.py`.

Python version used: Python 3.9.5

### Install guide on Unix Based systems (Here Windows WSL and MacOS was used)

On Mac you will need to Xcode installed (link)[https://apps.apple.com/us/app/xcode/id497799835?mt=12]

<br/>

You will need to install `blst` found in the `\blst` directory

Run `dos2unix blst/build.sh && bash blst/build.sh`

This will generate a `libblst.a` file in the `blst\bindings` folder

<br/>

Now you will need `swig` installed 

Run `sudo apt-get install swig` or `brew install swig` (on MacOS)
  
<br/>

After installing `swig` you need to compile the `blst.py` python file

Run `python blst\bindings\python\run.me`
  
<br/>

Now you will have a `blst.py` and `_blst.so` file in `blst\bindings\python`

These files needs to be moved to `/src` and now you should be able to run the implementations example `python src/vbst.py`

<br/>

The `.sh` shell scripts can be run to generate statistics for the different implementations fed with different parameters.

Run `bash {script_name}.sh` to execute







